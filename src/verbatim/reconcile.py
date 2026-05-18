"""Cross-session entity reconciliation.

When the same underlying commitment, decision, question, or blocker shows up
in two different sources (a meeting AND a Slack thread, two Slack threads on
different days, etc.), Verbatim should treat them as one entity with multiple
source quotes — not two separate items in the state graph.

This module implements the matching engine. The data model lives in `store.py`
(every entity has an optional `canonical_id` pointing to the canonical entity
of its group; canonical entities have `canonical_id IS NULL`).

# Matching policy

Two entities are eligible to be linked only if:
- they share a `kind` (commitments don't link to decisions)
- they share a normalized `primary_actor` (case-insensitive), if both have one
- their `primary_topic` strings are similar above a configurable threshold

We use `rapidfuzz.fuzz.token_set_ratio` for topic similarity — it's stable,
fast, no LLM call, and handles word-order differences and minor rewording.
The default threshold is conservative (88) because false-positive merges
silently rewrite the user's state and are hard to undo from the UI.

LLM-assisted reconciliation is intentionally out of scope here — it's a
follow-on if the deterministic version misses too many real duplicates.
For now we err strongly on the side of false negatives.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from . import store

DEFAULT_THRESHOLD = 88


@dataclass
class ReconcileMatch:
    candidate: dict[str, Any]
    score: int


@dataclass
class ReconcileResult:
    """One pass of reconciliation outcomes."""

    linked: int = 0  # entities newly merged
    skipped_unchanged: int = 0  # entities that were already canonical or already linked
    no_match: int = 0  # entities that found no candidate above threshold
    pairs: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.pairs is None:
            self.pairs = []


# ----------------------- candidate search -----------------------


def find_candidates(
    conn: sqlite3.Connection,
    entity: dict[str, Any],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = 10,
) -> list[ReconcileMatch]:
    """Find canonical entities of the same kind that look like duplicates of `entity`.

    Returns up to `limit` matches sorted by score descending. Only returns
    matches scoring at or above `threshold`. Does NOT include the entity itself.
    """
    if not entity.get("primary_topic"):
        return []
    candidates = store.fetch_entities(
        conn,
        kind=entity["kind"],
        primary_actor=entity.get("primary_actor"),
        status=None,
        canonical_only=True,
        limit=500,
    )
    matches: list[ReconcileMatch] = []
    for c in candidates:
        if c["id"] == entity["id"]:
            continue
        if not c.get("primary_topic"):
            continue
        score = topic_similarity(entity["primary_topic"], c["primary_topic"])
        if score >= threshold:
            matches.append(ReconcileMatch(candidate=c, score=score))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[:limit]


def topic_similarity(a: str, b: str) -> int:
    """Symmetric token-aware similarity in [0, 100]. Empty inputs score 0."""
    if not a or not b:
        return 0
    return int(fuzz.token_set_ratio(a.lower(), b.lower()))


# ----------------------- core linking -----------------------


def link_entities(
    conn: sqlite3.Connection,
    *,
    canonical_id: str,
    member_id: str,
) -> None:
    """Make `member_id` a member of `canonical_id`'s group.

    If `canonical_id` itself is already merged into another group, resolve
    upward and link `member_id` to the ultimate root. Refuses to create
    self-cycles.
    """
    if canonical_id == member_id:
        raise ValueError("Cannot link an entity to itself.")

    root = _resolve_canonical_root(conn, canonical_id)
    member = store.fetch_entity(conn, member_id)
    if member is None:
        raise ValueError(f"Member entity not found: {member_id}")

    if member["id"] == root:
        # already pointing at the same root via chain — treat as no-op
        return

    # If the member is itself a canonical with merged children, re-root them too.
    children = store.fetch_entities(
        conn, canonical_only=False, status=None, limit=1000
    )
    for child in children:
        if child.get("canonical_id") == member_id:
            store.set_canonical(conn, child["id"], root)

    store.set_canonical(conn, member_id, root)


def unlink_entity(conn: sqlite3.Connection, entity_id: str) -> bool:
    """Restore `entity_id` to standalone-canonical status. Returns True if changed."""
    entity = store.fetch_entity(conn, entity_id)
    if entity is None or entity.get("canonical_id") is None:
        return False
    store.clear_canonical(conn, entity_id)
    return True


def _resolve_canonical_root(conn: sqlite3.Connection, entity_id: str) -> str:
    """Walk canonical_id pointers until we find one that is itself canonical.

    Defends against pathological pointer chains by capping the walk.
    """
    seen: set[str] = set()
    current = entity_id
    for _ in range(50):  # defensive cap
        if current in seen:
            raise RuntimeError(f"Canonical cycle detected starting at {entity_id}")
        seen.add(current)
        row = conn.execute(
            "SELECT canonical_id FROM entities WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Entity not found: {current}")
        next_id = row["canonical_id"]
        if next_id is None:
            return current
        current = next_id
    raise RuntimeError(f"Canonical chain too deep starting at {entity_id}")


# ----------------------- batch reconciliation -----------------------


def reconcile_one(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[str, int] | None:
    """If a strong-enough match exists for this entity, merge it. Returns
    (canonical_id, score) of the chosen match, or None if no match.
    """
    entity = store.fetch_entity(conn, entity_id)
    if entity is None:
        return None
    if entity.get("canonical_id") is not None:
        return None  # already merged
    matches = find_candidates(conn, entity, threshold=threshold, limit=1)
    if not matches:
        return None
    best = matches[0]
    link_entities(conn, canonical_id=best.candidate["id"], member_id=entity_id)
    return best.candidate["id"], best.score


def reconcile_all(
    conn: sqlite3.Connection,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    kinds: list[str] | None = None,
) -> ReconcileResult:
    """Sweep over every standalone-canonical entity and link duplicates.

    Iteration order is created_at ASC so newer entities tend to merge into
    older ones — the older entity becomes the canonical, which keeps the
    audit trail readable (the merged entity is the "newer mention").
    """
    result = ReconcileResult()
    target_kinds = kinds or ["commitment", "decision", "open_question", "blocker"]
    for kind in target_kinds:
        rows = conn.execute(
            """
            SELECT id FROM entities
            WHERE kind = ? AND canonical_id IS NULL
            ORDER BY created_at ASC
            """,
            (kind,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        # Pass over in order. As we link, subsequent entities will find an
        # older canonical to merge into.
        for entity_id in ids:
            entity = store.fetch_entity(conn, entity_id)
            if entity is None or entity.get("canonical_id") is not None:
                result.skipped_unchanged += 1
                continue
            outcome = reconcile_one(conn, entity_id, threshold=threshold)
            if outcome is None:
                result.no_match += 1
            else:
                canonical_id, _score = outcome
                result.linked += 1
                result.pairs.append((canonical_id, entity_id))
    return result
