"""Domain-level operations on top of the raw SQLite store.

Translates between Pydantic ExtractionResult and the store's flat-row format,
and exposes high-level queries (list_commitments, search_entities, etc.).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
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


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    return store.connect(path)


def save_extraction(
    conn: sqlite3.Connection,
    result: ExtractionResult,
    diagnostics: ExtractionDiagnostics,
    *,
    source_path: str | None,
    source_kind: str = "transcript",
) -> IngestSummary:
    """Persist a complete ExtractionResult as one session + N entities + their sources.

    Idempotency note: v0.2 does not deduplicate against prior sessions. Every call
    creates a new session. Deduplication / reconciliation across sessions is a
    v1+ feature once we know what the right matching policy is.
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

    return IngestSummary(session_id=session_id, counts=counts)


def list_commitments(
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    min_confidence: str | None = None,
    status: str | None = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="commitment",
        primary_actor=actor,
        min_confidence=min_confidence,
        status=status,
        limit=limit,
    )


def list_decisions(
    conn: sqlite3.Connection,
    *,
    min_confidence: str | None = None,
    status: str | None = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="decision",
        min_confidence=min_confidence,
        status=status,
        limit=limit,
    )


def list_open_questions(
    conn: sqlite3.Connection,
    *,
    raised_by: str | None = None,
    min_confidence: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="open_question",
        primary_actor=raised_by,
        min_confidence=min_confidence,
        status="open",
        limit=limit,
    )


def list_blockers(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    min_confidence: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return store.fetch_entities(
        conn,
        kind="blocker",
        primary_actor=owner,
        min_confidence=min_confidence,
        status="open",
        limit=limit,
    )


def recent_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return store.fetch_recent_sessions(conn, limit=limit)


def resolve_entity(conn: sqlite3.Connection, entity_id: str) -> bool:
    return store.update_entity_status(conn, entity_id, "resolved")


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return store.db_stats(conn)


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
