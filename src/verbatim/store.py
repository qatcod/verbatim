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
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_actor ON entities(primary_actor);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
CREATE INDEX IF NOT EXISTS idx_entities_session ON entities(session_id);

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
"""


def resolve_db_path(path: str | Path | None = None) -> Path:
    """Resolve the DB path: explicit arg > $VERBATIM_DB_PATH > default."""
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("VERBATIM_DB_PATH")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection, ensure schema is initialized."""
    db_path = resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
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
    limit: int = 200,
) -> list[dict[str, Any]]:
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
               payload_json, created_at
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
        d["sources"] = fetch_sources(conn, d["id"])
        out.append(d)
    return out


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


def db_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Quick counts for status display."""
    out = {}
    out["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    for kind in ("commitment", "decision", "open_question", "blocker"):
        out[f"{kind}s_open"] = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE kind = ? AND status = 'open'",
            (kind,),
        ).fetchone()[0]
    return out
