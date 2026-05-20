"""Domain-level operations on top of the raw SQLite store.

Translates between Pydantic ExtractionResult and the store's flat-row format,
and exposes high-level queries (list_commitments, search_entities, etc.).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import deadlines, reconcile, store
from .extractor import ExtractionDiagnostics
from .schema import (
    Blocker,
    Commitment,
    Decision,
    ExtractionResult,
    OpenQuestion,
)


@dataclass
class IngestSummary:
    session_id: str
    counts: dict[str, int]
    reconcile_links: int = 0  # how many newly-saved entities got merged into existing canonicals


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    return store.connect(path)


def save_extraction(
    conn: sqlite3.Connection,
    result: ExtractionResult,
    diagnostics: ExtractionDiagnostics,
    *,
    source_path: str | None,
    source_kind: str = "transcript",
    auto_reconcile: bool = False,
    reconcile_threshold: int = reconcile.DEFAULT_THRESHOLD,
) -> IngestSummary:
    """Persist a complete ExtractionResult as one session + N entities + their sources.

    When `auto_reconcile=True`, every newly inserted entity is checked against
    existing canonical entities of the same kind and merged in if a strong-enough
    match exists (>= reconcile_threshold). The reconciliation count is returned
    on the IngestSummary.
    """
    with store.tx(conn):
        session_id = store.insert_session(
            conn,
            source_path=source_path,
            source_kind=source_kind,
            model=diagnostics.model,
            meeting_summary=result.meeting_summary,
            participants=result.participants,
            transcript_chars=diagnostics.transcript_chars,
            input_tokens=diagnostics.input_tokens,
            output_tokens=diagnostics.output_tokens,
        )

        counts = {"commitment": 0, "decision": 0, "open_question": 0, "blocker": 0}

        for c in result.commitments:
            entity_id = store.insert_entity(
                conn,
                session_id=session_id,
                kind="commitment",
                confidence=c.confidence.value,
                payload=_commitment_payload(c),
                primary_actor=c.actor,
                primary_topic=c.deliverable,
                deadline=c.deadline,
            )
            _persist_sources(conn, entity_id, c.sources)
            counts["commitment"] += 1

        for d in result.decisions:
            entity_id = store.insert_entity(
                conn,
                session_id=session_id,
                kind="decision",
                confidence=d.confidence.value,
                payload=_decision_payload(d),
                primary_actor=None,
                primary_topic=d.topic,
            )
            _persist_sources(conn, entity_id, d.sources)
            counts["decision"] += 1

        for q in result.open_questions:
            entity_id = store.insert_entity(
                conn,
                session_id=session_id,
                kind="open_question",
                confidence=q.confidence.value,
                payload=_question_payload(q),
                primary_actor=q.raised_by,
                primary_topic=q.topic,
            )
            _persist_sources(conn, entity_id, q.sources)
            counts["open_question"] += 1

        for b in result.blockers:
            entity_id = store.insert_entity(
                conn,
                session_id=session_id,
                kind="blocker",
                confidence=b.confidence.value,
                payload=_blocker_payload(b),
                primary_actor=b.owner,
                primary_topic=b.blocked_thing,
            )
            _persist_sources(conn, entity_id, b.sources)
            counts["blocker"] += 1

    summary = IngestSummary(session_id=session_id, counts=counts)

    if auto_reconcile:
        # Walk all entities created in this session and try to merge them.
        # We do this after the main transaction commits so the new entities
        # are visible to the candidate query.
        new_entity_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM entities WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        ]
        for eid in new_entity_ids:
            outcome = reconcile.reconcile_one(conn, eid, threshold=reconcile_threshold)
            if outcome is not None:
                summary.reconcile_links += 1

    return summary


def list_commitments(
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    min_confidence: str | None = None,
    status: str | None = "open",
    canonical_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="commitment",
        primary_actor=actor,
        min_confidence=min_confidence,
        status=status,
        canonical_only=canonical_only,
        limit=limit,
    )


def list_decisions(
    conn: sqlite3.Connection,
    *,
    min_confidence: str | None = None,
    status: str | None = "open",
    canonical_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="decision",
        min_confidence=min_confidence,
        status=status,
        canonical_only=canonical_only,
        limit=limit,
    )


def list_open_questions(
    conn: sqlite3.Connection,
    *,
    raised_by: str | None = None,
    min_confidence: str | None = None,
    canonical_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="open_question",
        primary_actor=raised_by,
        min_confidence=min_confidence,
        status="open",
        canonical_only=canonical_only,
        limit=limit,
    )


def list_blockers(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    min_confidence: str | None = None,
    canonical_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="blocker",
        primary_actor=owner,
        min_confidence=min_confidence,
        status="open",
        canonical_only=canonical_only,
        limit=limit,
    )


def show_entity(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    """Get a single entity by id; folds in merged siblings if it's canonical."""
    e = store.fetch_entity(conn, entity_id)
    if e is None:
        return None
    if e.get("canonical_id") is None:
        merged_ids = store._fetch_merged_member_ids(conn, e["id"])
        e["merged_count"] = len(merged_ids)
        for mid in merged_ids:
            e["sources"].extend(store.fetch_sources(conn, mid))
    else:
        e["merged_count"] = 0
    return e


def recent_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return store.fetch_recent_sessions(conn, limit=limit)


def resolve_entity(conn: sqlite3.Connection, entity_id: str) -> bool:
    return store.update_entity_status(conn, entity_id, "resolved")


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return store.db_stats(conn)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit_per_kind: int = 25,
) -> dict[str, list[dict[str, Any]]]:
    """Search across all entity kinds + source quotes.

    Returns a dict with keys: commitment, decision, open_question, blocker,
    source_match. Each value is a list of canonical entities matched. An
    entity matched only by a source quote appears in `source_match` and not
    in its kind-specific bucket (to avoid double-counting).

    Match is case-insensitive substring on: primary_actor, primary_topic,
    payload_json (everything else, JSON-stringified), and verbatim_quote.
    """
    q = query.strip()
    if not q:
        return {
            "commitment": [], "decision": [], "open_question": [],
            "blocker": [], "source_match": [],
        }
    return store.search_entities(conn, q, limit_per_kind=limit_per_kind)


# ----------------------- deadline tracking (proactive layer) -----------------------


def deadlined_commitments(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    within_days: int = deadlines.DUE_SOON_WINDOW_DAYS,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Open commitments annotated with `deadline_date` and `due_status`.

    Each returned dict is a normal commitment entity plus:
      - `deadline_date`: a `date` or None (parsed from the free-text deadline)
      - `due_status`: 'overdue' | 'due_today' | 'due_soon' | 'scheduled' | 'unknown'
      - `days_until`: signed int day delta, or None when the date didn't parse

    Sorted soonest-first: overdue at the top (most overdue first), then due
    today, then by ascending deadline; unparseable/undated last.
    """
    today = today or date.today()
    items = store.fetch_entities(
        conn, kind="commitment", status="open",
        canonical_only=True, limit=limit,
    )
    for item in items:
        raw = (item.get("payload") or {}).get("deadline") or item.get("deadline")
        parsed = deadlines.parse_deadline(raw, today=today)
        item["deadline_date"] = parsed
        item["due_status"] = deadlines.due_status(
            parsed, today=today, within_days=within_days,
        )
        item["days_until"] = (
            deadlines.days_until(parsed, today=today) if parsed else None
        )

    def _sort_key(it: dict[str, Any]) -> tuple[int, int]:
        d = it.get("deadline_date")
        if d is None:
            return (2, 0)  # undated → last
        return (0, (d - today).days)

    items.sort(key=_sort_key)
    return items


def overdue_commitments(
    conn: sqlite3.Connection, *, today: date | None = None, limit: int = 500,
) -> list[dict[str, Any]]:
    """Open commitments whose parsed deadline is in the past."""
    return [
        c for c in deadlined_commitments(conn, today=today, limit=limit)
        if c["due_status"] == "overdue"
    ]


def due_soon_commitments(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    within_days: int = deadlines.DUE_SOON_WINDOW_DAYS,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Open commitments due today or within `within_days` days."""
    return [
        c for c in deadlined_commitments(
            conn, today=today, within_days=within_days, limit=limit,
        )
        if c["due_status"] in ("due_today", "due_soon")
    ]


# ----------------------- staleness + standup (proactive layer) -----------------------


def stale_entities(
    conn: sqlite3.Connection,
    *,
    stale_after_days: int = 30,
    kind: str | None = None,
    now: datetime | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Open entities that have sat untouched longer than `stale_after_days`.

    "Untouched" means no audit activity (confirm / edit / reassign / dismiss)
    since the cutoff. Each returned entity carries `last_activity` (ISO string
    or None) and `idle_days` (whole days since it was created).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_after_days)
    cutoff_iso = cutoff.isoformat()
    items = store.fetch_stale_entities(
        conn, before_iso=cutoff_iso, kind=kind, limit=limit,
    )
    for item in items:
        created = item.get("created_at")
        try:
            created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            item["idle_days"] = (now - created_dt).days
        except (ValueError, TypeError):
            item["idle_days"] = None
    return items


def standup(
    conn: sqlite3.Connection,
    person: str,
    *,
    now: datetime | None = None,
    recent_days: int = 7,
) -> dict[str, Any]:
    """Build a standup-style status summary for one person from the state graph.

    Returns a dict with:
      - `owed`: open commitments where the person is the actor (deadline-sorted)
      - `blocked`: blockers the person owns
      - `questions`: open questions the person raised
      - `recently_resolved`: entities tied to the person whose audit log shows
        a confirm/resolve/dismiss within `recent_days`
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    view = store.fetch_person(conn, person, include_resolved=False)

    owed = deadlined_commitments_subset(view["commitments"], today=today)

    recent_cutoff = (now - timedelta(days=recent_days)).isoformat()
    recently_resolved: list[dict[str, Any]] = []
    # Audit-driven "what moved recently" — check every entity tied to the person.
    seen: set[str] = set()
    for bucket in (view["commitments"], view["decisions"],
                   view["questions_raised"], view["blockers_owned"]):
        for entity in bucket:
            if entity["id"] in seen:
                continue
            seen.add(entity["id"])
            audit = store.fetch_audit(conn, entity["id"], limit=20)
            for row in audit:
                if (row["action"] in ("confirm", "resolve", "dismiss")
                        and (row.get("created_at") or "") >= recent_cutoff):
                    recently_resolved.append({
                        "entity": entity,
                        "action": row["action"],
                        "at": row.get("created_at"),
                    })
                    break

    return {
        "person": person,
        "owed": owed,
        "blocked": view["blockers_owned"],
        "questions": view["questions_raised"],
        "recently_resolved": recently_resolved,
        "stats": view["stats"],
    }


def deadlined_commitments_subset(
    commitments: list[dict[str, Any]], *, today: date | None = None,
) -> list[dict[str, Any]]:
    """Annotate an already-fetched commitment list with deadline status.

    A lighter sibling of `deadlined_commitments` for callers (like `standup`)
    that already hold the commitment rows and don't want a second DB query.
    """
    today = today or date.today()
    for item in commitments:
        raw = (item.get("payload") or {}).get("deadline") or item.get("deadline")
        parsed = deadlines.parse_deadline(raw, today=today)
        item["deadline_date"] = parsed
        item["due_status"] = deadlines.due_status(parsed, today=today)
        item["days_until"] = (
            deadlines.days_until(parsed, today=today) if parsed else None
        )

    def _key(it: dict[str, Any]) -> tuple[int, int]:
        d = it.get("deadline_date")
        return (2, 0) if d is None else (0, (d - today).days)

    return sorted(commitments, key=_key)


# Payload serializers — preserve the kind-specific fields not in the
# denormalized columns. Kept in one place so payload schema is auditable.


def _commitment_payload(c: Commitment) -> dict[str, Any]:
    return {
        "actor": c.actor,
        "deliverable": c.deliverable,
        "deadline": c.deadline,
        "to": c.to,
        "notes": c.notes,
    }


def _decision_payload(d: Decision) -> dict[str, Any]:
    return {
        "topic": d.topic,
        "outcome": d.outcome,
        "participants": d.participants,
        "rationale": d.rationale,
        "alternatives_considered": d.alternatives_considered,
    }


def _question_payload(q: OpenQuestion) -> dict[str, Any]:
    return {
        "topic": q.topic,
        "question": q.question,
        "raised_by": q.raised_by,
        "addressed_to": q.addressed_to,
        "urgency": q.urgency,
    }


def _blocker_payload(b: Blocker) -> dict[str, Any]:
    return {
        "blocked_thing": b.blocked_thing,
        "blocked_by": b.blocked_by,
        "owner": b.owner,
    }


def _persist_sources(conn: sqlite3.Connection, entity_id: str, sources) -> None:
    for i, s in enumerate(sources):
        store.insert_source(
            conn,
            entity_id=entity_id,
            seq=i,
            verbatim_quote=s.verbatim_quote,
            speaker=s.speaker,
            approximate_timestamp=s.approximate_timestamp,
            rationale=s.rationale,
        )
