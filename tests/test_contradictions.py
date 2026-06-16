"""Tests for v0.11.2 — contradiction detection (same topic, different outcome)."""
from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from verbatim import contradictions, state, web
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Confidence,
    Decision,
    ExtractionResult,
    SourceReference,
)


def _diag() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )


def _seed_decisions(db_path: Path, decisions: list[tuple[str, str]]) -> None:
    """Seed decisions as (topic, outcome) tuples."""
    conn = state.open_db(db_path)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Alice"],
                decisions=[
                    Decision(
                        topic=topic, outcome=outcome, participants=["Alice"],
                        confidence=Confidence.HIGH,
                        sources=[SourceReference(
                            verbatim_quote=f"{topic}: {outcome}",
                            speaker="Alice", rationale="r",
                        )],
                    )
                    for topic, outcome in decisions
                ],
            ),
            _diag(), source_path="m.txt",
        )
    finally:
        conn.close()


def test_detects_same_topic_different_outcome(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("database choice for the backend", "use SQLite instead"),
    ])
    conn = state.open_db(db)
    try:
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert len(pairs) == 1
    assert pairs[0].topic_score >= 75


def test_no_contradiction_for_same_outcome(tmp_path: Path) -> None:
    """Same topic + same outcome is a duplicate, not a contradiction."""
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("database choice for the backend", "use Postgres"),
    ])
    conn = state.open_db(db)
    try:
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert pairs == []


def test_no_contradiction_for_unrelated_topics(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("frontend framework selection", "use React"),
    ])
    conn = state.open_db(db)
    try:
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert pairs == []


def test_skips_decisions_with_empty_outcome(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("database choice for the backend", ""),
    ])
    conn = state.open_db(db)
    try:
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert pairs == []


def test_resolved_decisions_excluded(tmp_path: Path) -> None:
    """Only open decisions are scanned for contradictions."""
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("database choice for the backend", "use SQLite instead"),
    ])
    conn = state.open_db(db)
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE kind='decision' LIMIT 1"
        ).fetchone()
        state.resolve_entity(conn, row["id"])
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert pairs == []


def test_topic_property_returns_decision_topic(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("API versioning strategy", "put the version in the URL path"),
        ("API versioning strategy", "send an Accept header field"),
    ])
    conn = state.open_db(db)
    try:
        pairs = contradictions.find_contradictions(conn)
    finally:
        conn.close()
    assert len(pairs) == 1
    assert "versioning" in pairs[0].topic.lower()


def test_threshold_tuning_widens_matches(tmp_path: Path) -> None:
    """A loose topic threshold catches more loosely-related decisions."""
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice", "use Postgres"),
        ("the database decision", "use MongoDB"),
    ])
    conn = state.open_db(db)
    try:
        strict = contradictions.find_contradictions(conn, topic_threshold=95)
        loose = contradictions.find_contradictions(conn, topic_threshold=55)
    finally:
        conn.close()
    assert len(loose) >= len(strict)


# ----- web route -----


def test_contradictions_route_renders(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed_decisions(db, [
        ("database choice for the backend", "use Postgres"),
        ("database choice for the backend", "use SQLite instead"),
    ])
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/contradictions")
    assert r.status_code == 200
    assert "Contradictions" in r.text
    assert "Postgres" in r.text
    assert "SQLite" in r.text


def test_contradictions_route_empty_state(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    state.open_db(db).close()
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/contradictions")
    assert r.status_code == 200
    assert "No contradictions found" in r.text


def test_contradictions_link_in_sidebar(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    state.open_db(db).close()
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/")
    assert 'href="/contradictions"' in r.text
