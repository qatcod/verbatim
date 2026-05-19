"""SQLite-backed persistence for Verbatim state.

The store is intentionally simple in v0.2: three tables (sessions, entities,
entity_sources). Entity payloads are stored as JSON for schema flexibility;
a few common fields are denormalized into columns for fast filtering.

The state graph (reconciliation across sessions, identity resolution,
relationship inference) is built on top of this layer in `state.py`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".verbatim" / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source_path TEXT,
    source_kind TEXT NOT NULL,
    model TEXT NOT NULL,
    meeting_summary TEXT,
    participants_json TEXT NOT NULL DEFAULT '[]',
    extracted_at TEXT NOT NULL,
    transcript_chars INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    primary_actor TEXT,
    primary_topic TEXT,
    deadline TEXT,
    payload_json TEXT NOT NULL,
    canonical_id TEXT,
    merged_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_id) REFERENCES entities(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_actor ON entities(primary_actor);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
CREATE INDEX IF NOT EXISTS idx_entities_session ON entities(session_id);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_id);

CREATE TABLE IF NOT EXISTS entity_sources (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    verbatim_quote TEXT NOT NULL,
    speaker TEXT,
    approximate_timestamp TEXT,
    rationale TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sources_entity ON entity_sources(entity_id);

CREATE TABLE IF NOT EXISTS projections (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    external_id TEXT,
    external_url TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_projections_entity ON projections(entity_id);
CREATE INDEX IF NOT EXISTS idx_projections_target ON projections(target_kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projections_unique
    ON projections(entity_id, target_kind, status);

CREATE TABLE IF NOT EXISTS entity_audit (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT,
    actor_label TEXT,
    before_json TEXT,
    after_json TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON entity_audit(entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON entity_audit(created_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations for DBs created by earlier versions.

    Pre-0.4.0 DBs have entities without canonical_id / merged_at columns;
    add them in-place. CREATE TABLE IF NOT EXISTS covers fresh DBs.
    """
    existing_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(entities)").fetchall()
    }
    if "canonical_id" not in existing_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN canonical_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_id)")
    if "merged_at" not in existing_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN merged_at TEXT")

    # entity_audit table (added in v0.10.1). Older DBs predate the SCHEMA
    # declaration, so an explicit CREATE TABLE IF NOT EXISTS keeps them
    # working without a forced re-create. CREATE INDEX IF NOT EXISTS covers
    # the index too.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_audit (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_id TEXT,
            actor_label TEXT,
            before_json TEXT,
            after_json TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_entity ON entity_audit(entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON entity_audit(created_at)"
    )


def resolve_db_path(path: str | Path | None = None) -> Path:
    """Resolve the DB path: explicit arg > $VERBATIM_DB_PATH > default."""
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("VERBATIM_DB_PATH")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection, ensure schema is initialized + migrated."""
    db_path = resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a block in an immediate transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_session(
    conn: sqlite3.Connection,
    *,
    source_path: str | None,
    source_kind: str,
    model: str,
    meeting_summary: str,
    participants: list[str],
    transcript_chars: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    session_id = new_id()
    conn.execute(
        """
        INSERT INTO sessions (
            id, source_path, source_kind, model, meeting_summary,
            participants_json, extracted_at, transcript_chars,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source_path,
            source_kind,
            model,
            meeting_summary,
            json.dumps(participants, ensure_ascii=False),
            utc_now_iso(),
            transcript_chars,
            input_tokens,
            output_tokens,
        ),
    )
    return session_id


def insert_entity(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    kind: str,
    confidence: str,
    payload: dict[str, Any],
    primary_actor: str | None,
    primary_topic: str | None,
    deadline: str | None = None,
    status: str = "open",
) -> str:
    entity_id = new_id()
    conn.execute(
        """
        INSERT INTO entities (
            id, session_id, kind, confidence, status,
            primary_actor, primary_topic, deadline,
            payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            session_id,
            kind,
            confidence,
            status,
            primary_actor,
            primary_topic,
            deadline,
            json.dumps(payload, ensure_ascii=False),
            utc_now_iso(),
        ),
    )
    return entity_id


def insert_source(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    seq: int,
    verbatim_quote: str,
    speaker: str | None,
    approximate_timestamp: str | None,
    rationale: str,
) -> str:
    source_id = new_id()
    conn.execute(
        """
        INSERT INTO entity_sources (
            id, entity_id, seq, verbatim_quote, speaker,
            approximate_timestamp, rationale
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            entity_id,
            seq,
            verbatim_quote,
            speaker,
            approximate_timestamp,
            rationale,
        ),
    )
    return source_id


def fetch_sources(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT verbatim_quote, speaker, approximate_timestamp, rationale, seq
        FROM entity_sources
        WHERE entity_id = ?
        ORDER BY seq ASC
        """,
        (entity_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_entities(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    primary_actor: str | None = None,
    status: str | None = "open",
    min_confidence: str | None = None,
    session_id: str | None = None,
    canonical_only: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch entities matching the given filters.

    By default returns canonical entities only (rows where `canonical_id IS NULL`),
    which is the right view for "show me what's live". For each canonical entity
    returned, `sources` includes quotes from any merged siblings, and
    `merged_count` reports how many siblings are linked.

    Pass `canonical_only=False` to see every entity individually (no group folding).
    """
    conditions: list[str] = []
    params: list[Any] = []
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if primary_actor:
        conditions.append("LOWER(primary_actor) = LOWER(?)")
        params.append(primary_actor)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if canonical_only:
        conditions.append("canonical_id IS NULL")
    if min_confidence:
        order = {"low": 0, "medium": 1, "high": 2}
        threshold = order.get(min_confidence.lower(), 0)
        allowed = [k for k, v in order.items() if v >= threshold]
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"confidence IN ({placeholders})")
        params.extend(allowed)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT id, session_id, kind, confidence, status,
               primary_actor, primary_topic, deadline,
               payload_json, canonical_id, merged_at, created_at
        FROM entities
        {where}
        ORDER BY created_at DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(query, params).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        if canonical_only:
            # Fold sources from any merged siblings into this canonical's sources.
            merged_ids = _fetch_merged_member_ids(conn, d["id"])
            d["merged_count"] = len(merged_ids)
            d["sources"] = fetch_sources(conn, d["id"])
            for mid in merged_ids:
                d["sources"].extend(fetch_sources(conn, mid))
        else:
            d["merged_count"] = 0
            d["sources"] = fetch_sources(conn, d["id"])
        out.append(d)
    return out


def _fetch_merged_member_ids(conn: sqlite3.Connection, canonical_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM entities WHERE canonical_id = ? ORDER BY created_at ASC",
        (canonical_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def fetch_entity(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    """Fetch a single entity by id, no folding."""
    row = conn.execute(
        """
        SELECT id, session_id, kind, confidence, status,
               primary_actor, primary_topic, deadline,
               payload_json, canonical_id, merged_at, created_at
        FROM entities
        WHERE id = ?
        """,
        (entity_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    d["sources"] = fetch_sources(conn, d["id"])
    return d


def set_canonical(
    conn: sqlite3.Connection,
    entity_id: str,
    canonical_id: str,
) -> None:
    """Mark `entity_id` as a member of `canonical_id`'s group (non-canonical)."""
    conn.execute(
        "UPDATE entities SET canonical_id = ?, merged_at = ? WHERE id = ?",
        (canonical_id, utc_now_iso(), entity_id),
    )


def clear_canonical(conn: sqlite3.Connection, entity_id: str) -> None:
    """Restore `entity_id` to standalone-canonical status."""
    conn.execute(
        "UPDATE entities SET canonical_id = NULL, merged_at = NULL WHERE id = ?",
        (entity_id,),
    )


def fetch_recent_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM entities e WHERE e.session_id = s.id) AS entity_count
        FROM sessions s
        ORDER BY s.extracted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["participants"] = json.loads(d.pop("participants_json"))
        out.append(d)
    return out


def update_entity_status(conn: sqlite3.Connection, entity_id: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE entities SET status = ? WHERE id = ?",
        (status, entity_id),
    )
    return cur.rowcount > 0


# ----------------------- entity field updates + audit log -----------------------


def update_entity_fields(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    primary_actor: str | None = None,
    primary_topic: str | None = None,
    deadline: str | None = None,
    payload_overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Update one entity's fields and rewrite payload_json from `payload_overrides`.

    Returns the (before, after) snapshot if the row existed, else None.
    Callers should pair this with `record_audit` for traceability.
    """
    row = conn.execute(
        "SELECT primary_actor, primary_topic, deadline, payload_json "
        "FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    before = {
        "primary_actor": row["primary_actor"],
        "primary_topic": row["primary_topic"],
        "deadline": row["deadline"],
        "payload": json.loads(row["payload_json"]),
    }
    new_actor = primary_actor if primary_actor is not None else row["primary_actor"]
    new_topic = primary_topic if primary_topic is not None else row["primary_topic"]
    new_deadline = deadline if deadline is not None else row["deadline"]
    new_payload = dict(before["payload"])
    if payload_overrides:
        new_payload.update({k: v for k, v in payload_overrides.items() if v is not None})

    conn.execute(
        "UPDATE entities SET primary_actor = ?, primary_topic = ?, "
        "deadline = ?, payload_json = ? WHERE id = ?",
        (
            new_actor, new_topic, new_deadline,
            json.dumps(new_payload, ensure_ascii=False),
            entity_id,
        ),
    )
    return {
        "before": before,
        "after": {
            "primary_actor": new_actor,
            "primary_topic": new_topic,
            "deadline": new_deadline,
            "payload": new_payload,
        },
    }


def record_audit(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    action: str,
    actor_id: str | None = None,
    actor_label: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    note: str | None = None,
) -> str:
    """Write one audit-log row. Returns the new row's id.

    `action` is a short verb: 'confirm', 'dismiss', 'edit', 'reassign',
    'resolve', 'create', 'merge', 'unlink'. The before/after dicts are
    serialized as JSON for replay.
    """
    audit_id = new_id()
    conn.execute(
        """
        INSERT INTO entity_audit (
            id, entity_id, action, actor_id, actor_label,
            before_json, after_json, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id, entity_id, action, actor_id, actor_label,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            note, utc_now_iso(),
        ),
    )
    return audit_id


def fetch_audit(
    conn: sqlite3.Connection, entity_id: str, *, limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the audit log for one entity, newest first."""
    rows = conn.execute(
        """
        SELECT id, entity_id, action, actor_id, actor_label,
               before_json, after_json, note, created_at
        FROM entity_audit
        WHERE entity_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (entity_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["before"] = json.loads(d.pop("before_json")) if d.get("before_json") else None
        d["after"] = json.loads(d.pop("after_json")) if d.get("after_json") else None
        out.append(d)
    return out


def db_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Quick counts for status display. Counts canonical entities only."""
    out = {}
    out["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    for kind in ("commitment", "decision", "open_question", "blocker"):
        out[f"{kind}s_open"] = conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE kind = ? AND status = 'open' AND canonical_id IS NULL",
            (kind,),
        ).fetchone()[0]
    out["entities_merged"] = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE canonical_id IS NOT NULL"
    ).fetchone()[0]
    out["projections_active"] = conn.execute(
        "SELECT COUNT(*) FROM projections WHERE status = 'active'"
    ).fetchone()[0]
    return out


# ----------------------- projection CRUD -----------------------


def insert_projection(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    target_kind: str,
    external_id: str | None,
    external_url: str | None,
    metadata: dict[str, Any] | None = None,
) -> str:
    projection_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO projections (
            id, entity_id, target_kind, external_id, external_url,
            status, metadata_json, created_at, last_synced_at
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            projection_id,
            entity_id,
            target_kind,
            external_id,
            external_url,
            json.dumps(metadata or {}, ensure_ascii=False),
            now,
            now,
        ),
    )
    return projection_id


def find_active_projection(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    target_kind: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, entity_id, target_kind, external_id, external_url,
               status, metadata_json, created_at, last_synced_at
        FROM projections
        WHERE entity_id = ? AND target_kind = ? AND status = 'active'
        """,
        (entity_id, target_kind),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metadata"] = json.loads(d.pop("metadata_json"))
    return d


def list_projections(
    conn: sqlite3.Connection,
    *,
    target_kind: str | None = None,
    status: str | None = "active",
    limit: int = 200,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if target_kind:
        conditions.append("p.target_kind = ?")
        params.append(target_kind)
    if status is not None:
        conditions.append("p.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"""
        SELECT p.id, p.entity_id, p.target_kind, p.external_id, p.external_url,
               p.status, p.metadata_json, p.created_at, p.last_synced_at,
               e.kind AS entity_kind, e.primary_actor, e.primary_topic, e.confidence
        FROM projections p
        LEFT JOIN entities e ON e.id = p.entity_id
        {where}
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["metadata"] = json.loads(d.pop("metadata_json"))
        out.append(d)
    return out


def update_projection_status(
    conn: sqlite3.Connection,
    projection_id: str,
    status: str,
) -> bool:
    cur = conn.execute(
        "UPDATE projections SET status = ?, last_synced_at = ? WHERE id = ?",
        (status, utc_now_iso(), projection_id),
    )
    return cur.rowcount > 0


# ----------------------- search -----------------------


def search_entities(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit_per_kind: int = 25,
) -> dict[str, list[dict[str, Any]]]:
    """Cross-kind substring search.

    Direct entity matches (actor/topic/payload) are bucketed by kind.
    Entities matched only via a source quote land in `source_match`,
    deduped against the direct buckets.
    """
    like = f"%{query}%"
    out: dict[str, list[dict[str, Any]]] = {
        "commitment": [], "decision": [], "open_question": [],
        "blocker": [], "source_match": [],
    }
    direct_ids: set[str] = set()

    for kind in ("commitment", "decision", "open_question", "blocker"):
        rows = conn.execute(
            """
            SELECT id FROM entities
            WHERE kind = ?
              AND canonical_id IS NULL
              AND (
                  primary_actor LIKE ? COLLATE NOCASE
                  OR primary_topic LIKE ? COLLATE NOCASE
                  OR payload_json LIKE ? COLLATE NOCASE
              )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (kind, like, like, like, limit_per_kind),
        ).fetchall()
        for r in rows:
            entity = fetch_entity(conn, r["id"])
            if entity:
                merged_ids = _fetch_merged_member_ids(conn, entity["id"])
                entity["merged_count"] = len(merged_ids)
                for mid in merged_ids:
                    entity["sources"].extend(fetch_sources(conn, mid))
                out[kind].append(entity)
                direct_ids.add(entity["id"])

    # Quote-only matches: entities whose source-text matched but whose direct
    # fields didn't.
    quote_rows = conn.execute(
        """
        SELECT DISTINCT e.id, e.created_at FROM entity_sources es
        JOIN entities e ON e.id = es.entity_id
        WHERE es.verbatim_quote LIKE ? COLLATE NOCASE
          AND e.canonical_id IS NULL
        ORDER BY e.created_at DESC
        LIMIT ?
        """,
        (like, limit_per_kind * 4),
    ).fetchall()
    for r in quote_rows:
        if r["id"] in direct_ids:
            continue
        entity = fetch_entity(conn, r["id"])
        if entity:
            merged_ids = _fetch_merged_member_ids(conn, entity["id"])
            entity["merged_count"] = len(merged_ids)
            for mid in merged_ids:
                entity["sources"].extend(fetch_sources(conn, mid))
            out["source_match"].append(entity)
    return out


# ----------------------- person view -----------------------


def fetch_person(
    conn: sqlite3.Connection,
    name: str,
    *,
    include_resolved: bool = False,
    limit_per_kind: int = 100,
) -> dict[str, Any]:
    """Aggregate everything tied to a person.

    Resolves three buckets via `primary_actor` (the kind's natural anchor —
    commitment.actor, open_question.raised_by, blocker.owner) plus a
    JSON1-extracted bucket for decisions where the person appears in
    `payload.participants`. Match is case-insensitive substring on
    `primary_actor`, so 'qat' resolves 'Qat' / 'Qatadah' / 'qatcod'.
    """
    like = f"%{name}%"
    status_clause = "" if include_resolved else " AND e.status = 'open'"

    # commitments / questions / blockers: primary_actor is the anchor
    commitments = _fetch_entities_by_actor(
        conn, kind="commitment", like=like, limit=limit_per_kind,
        status_clause=status_clause,
    )
    questions = _fetch_entities_by_actor(
        conn, kind="open_question", like=like, limit=limit_per_kind,
        status_clause=status_clause,
    )
    blockers = _fetch_entities_by_actor(
        conn, kind="blocker", like=like, limit=limit_per_kind,
        status_clause=status_clause,
    )

    # decisions: participants array in payload_json; use JSON1
    decision_rows = conn.execute(
        f"""
        SELECT DISTINCT e.id
        FROM entities e, json_each(e.payload_json, '$.participants') AS p
        WHERE e.kind = 'decision'
          AND e.canonical_id IS NULL
          AND p.value LIKE ? COLLATE NOCASE
          {status_clause}
        ORDER BY e.created_at DESC
        LIMIT ?
        """,
        (like, limit_per_kind),
    ).fetchall()
    decisions: list[dict[str, Any]] = []
    for r in decision_rows:
        entity = fetch_entity(conn, r["id"])
        if entity is None:
            continue
        merged_ids = _fetch_merged_member_ids(conn, entity["id"])
        entity["merged_count"] = len(merged_ids)
        for mid in merged_ids:
            entity["sources"].extend(fetch_sources(conn, mid))
        decisions.append(entity)

    return {
        "name": name,
        "commitments": commitments,
        "decisions": decisions,
        "questions_raised": questions,
        "blockers_owned": blockers,
        "stats": {
            "commitments": len(commitments),
            "decisions": len(decisions),
            "questions_raised": len(questions),
            "blockers_owned": len(blockers),
            "total": (len(commitments) + len(decisions)
                      + len(questions) + len(blockers)),
        },
    }


def _fetch_entities_by_actor(
    conn: sqlite3.Connection,
    *,
    kind: str,
    like: str,
    limit: int,
    status_clause: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT e.id FROM entities e
        WHERE e.kind = ?
          AND e.canonical_id IS NULL
          AND e.primary_actor LIKE ? COLLATE NOCASE
          {status_clause}
        ORDER BY e.created_at DESC
        LIMIT ?
        """,
        (kind, like, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        entity = fetch_entity(conn, r["id"])
        if entity is None:
            continue
        merged_ids = _fetch_merged_member_ids(conn, entity["id"])
        entity["merged_count"] = len(merged_ids)
        for mid in merged_ids:
            entity["sources"].extend(fetch_sources(conn, mid))
        out.append(entity)
    return out


def list_known_people(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Distinct people who appear in the state graph.

    Walks `primary_actor` across all open canonical entities and counts
    occurrences per name. Used to populate `/people` and the person picker.
    Names that differ only in case are folded into the most-frequent variant.
    """
    rows = conn.execute(
        """
        SELECT primary_actor AS name, COUNT(*) AS total
        FROM entities
        WHERE canonical_id IS NULL
          AND primary_actor IS NOT NULL
          AND primary_actor <> ''
        GROUP BY primary_actor
        ORDER BY total DESC, name COLLATE NOCASE ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen_lower: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = (r["name"] or "").strip().lower()
        if not key:
            continue
        if key in seen_lower:
            seen_lower[key]["total"] += r["total"]
            continue
        item = {"name": r["name"], "total": r["total"]}
        seen_lower[key] = item
        out.append(item)
    out.sort(key=lambda x: (-x["total"], x["name"].lower()))
    return out
