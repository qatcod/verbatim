"""Tests for v0.11.1 — staleness detection (state.stale_entities) and the
auto-standup generator (state.standup)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verbatim import state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)

NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _diag() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )


def _backdate_entities(conn: sqlite3.Connection, days_ago: int) -> None:
    """Rewrite every entity's created_at to `days_ago` days before NOW."""
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    conn.execute("UPDATE entities SET created_at = ?", (ts,))


# ----- staleness -----


def test_stale_entities_flags_old_untouched(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[Commitment(
                    actor="Qat", deliverable="old thing",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r",
                    )],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        _backdate_entities(conn, days_ago=60)
        stale = state.stale_entities(conn, stale_after_days=30, now=NOW)
    finally:
        conn.close()
    assert len(stale) == 1
    assert stale[0]["payload"]["deliverable"] == "old thing"
    assert stale[0]["idle_days"] == 60


def test_stale_entities_excludes_recent(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[Commitment(
                    actor="Qat", deliverable="fresh thing",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r",
                    )],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        _backdate_entities(conn, days_ago=5)
        stale = state.stale_entities(conn, stale_after_days=30, now=NOW)
    finally:
        conn.close()
    assert stale == []


def test_stale_entities_excludes_recently_touched(tmp_path: Path) -> None:
    """An old entity with a recent audit row is not stale."""
    db = tmp_path / "s.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[Commitment(
                    actor="Qat", deliverable="touched thing",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r",
                    )],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        _backdate_entities(conn, days_ago=60)
        eid = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()["id"]
        # A confirm action 2 days ago — recent activity.
        conn.execute(
            "INSERT INTO entity_audit (id, entity_id, action, created_at) "
            "VALUES (?, ?, 'confirm', ?)",
            (store.new_id(), eid, (NOW - timedelta(days=2)).isoformat()),
        )
        stale = state.stale_entities(conn, stale_after_days=30, now=NOW)
    finally:
        conn.close()
    assert stale == []


def test_stale_entities_kind_filter(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[Commitment(
                    actor="Qat", deliverable="c", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="b", blocked_by="x", owner="Qat",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        _backdate_entities(conn, days_ago=60)
        only_blockers = state.stale_entities(
            conn, stale_after_days=30, kind="blocker", now=NOW,
        )
    finally:
        conn.close()
    assert len(only_blockers) == 1
    assert only_blockers[0]["kind"] == "blocker"


# ----- standup -----


def _seed_full(db_path: Path) -> None:
    conn = state.open_db(db_path)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat", "Jason"],
                commitments=[Commitment(
                    actor="Qat", deliverable="ship the launch",
                    deadline="2026-05-25", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
                decisions=[Decision(
                    topic="db", outcome="Postgres", participants=["Qat"],
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
                open_questions=[OpenQuestion(
                    topic="ops", question="who runs ops?", raised_by="Qat",
                    confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="release", blocked_by="security review",
                    owner="Qat", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Qat", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
    finally:
        conn.close()


def test_standup_collects_all_buckets(tmp_path: Path) -> None:
    db = tmp_path / "su.db"
    _seed_full(db)
    conn = state.open_db(db)
    try:
        report = state.standup(conn, "Qat", now=NOW)
    finally:
        conn.close()
    assert report["person"] == "Qat"
    assert len(report["owed"]) == 1
    assert report["owed"][0]["payload"]["deliverable"] == "ship the launch"
    assert len(report["blocked"]) == 1
    assert len(report["questions"]) == 1


def test_standup_owed_is_deadline_annotated(tmp_path: Path) -> None:
    db = tmp_path / "su.db"
    _seed_full(db)
    conn = state.open_db(db)
    try:
        report = state.standup(conn, "Qat", now=NOW)
    finally:
        conn.close()
    owed = report["owed"][0]
    assert "due_status" in owed
    assert "days_until" in owed


def test_standup_recently_resolved_from_audit(tmp_path: Path) -> None:
    db = tmp_path / "su.db"
    _seed_full(db)
    conn = state.open_db(db)
    try:
        eid = conn.execute(
            "SELECT id FROM entities WHERE kind='commitment'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO entity_audit (id, entity_id, action, created_at) "
            "VALUES (?, ?, 'confirm', ?)",
            (store.new_id(), eid, (NOW - timedelta(days=1)).isoformat()),
        )
        report = state.standup(conn, "Qat", now=NOW)
    finally:
        conn.close()
    assert len(report["recently_resolved"]) == 1
    assert report["recently_resolved"][0]["action"] == "confirm"


def test_standup_ignores_old_audit_activity(tmp_path: Path) -> None:
    db = tmp_path / "su.db"
    _seed_full(db)
    conn = state.open_db(db)
    try:
        eid = conn.execute(
            "SELECT id FROM entities WHERE kind='commitment'"
        ).fetchone()["id"]
        # A confirm 30 days ago — outside the default 7-day recent window.
        conn.execute(
            "INSERT INTO entity_audit (id, entity_id, action, created_at) "
            "VALUES (?, ?, 'confirm', ?)",
            (store.new_id(), eid, (NOW - timedelta(days=30)).isoformat()),
        )
        report = state.standup(conn, "Qat", now=NOW)
    finally:
        conn.close()
    assert report["recently_resolved"] == []


def test_standup_unknown_person_is_empty(tmp_path: Path) -> None:
    db = tmp_path / "su.db"
    _seed_full(db)
    conn = state.open_db(db)
    try:
        report = state.standup(conn, "Nobody", now=NOW)
    finally:
        conn.close()
    assert report["stats"]["total"] == 0
    assert report["owed"] == []
    assert report["recently_resolved"] == []
