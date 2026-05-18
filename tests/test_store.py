"""SQLite store tests — schema bootstrap and CRUD."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from verbatim import store


def test_schema_initialized_on_connect(tmp_db_path: Path) -> None:
    conn = store.connect(tmp_db_path)
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"sessions", "entities", "entity_sources"} <= tables
    conn.close()


def test_resolve_db_path_uses_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERBATIM_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.delenv("HOME", raising=False)
    p = store.resolve_db_path()
    assert p == tmp_path / "env.db"


def test_insert_session_returns_id(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(
        conn, source_path="meeting.txt", source_kind="transcript",
        model="claude-sonnet-4-6", meeting_summary="x",
        participants=["A", "B"], transcript_chars=100,
        input_tokens=50, output_tokens=25,
    )
    assert sid
    rows = conn.execute("SELECT id, meeting_summary FROM sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["meeting_summary"] == "x"


def test_insert_entity_and_sources(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(
        conn, source_path=None, source_kind="transcript",
        model="m", meeting_summary="", participants=[],
    )
    eid = store.insert_entity(
        conn, session_id=sid, kind="commitment", confidence="high",
        payload={"actor": "Qat", "deliverable": "thing"},
        primary_actor="Qat", primary_topic="thing", deadline="Friday",
    )
    store.insert_source(
        conn, entity_id=eid, seq=0, verbatim_quote="quoted",
        speaker="Qat", approximate_timestamp=None, rationale="because",
    )
    sources = store.fetch_sources(conn, eid)
    assert len(sources) == 1
    assert sources[0]["verbatim_quote"] == "quoted"


def test_fetch_entities_filters_by_kind(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    store.insert_entity(conn, session_id=sid, kind="commitment", confidence="high",
                        payload={}, primary_actor="A", primary_topic="x")
    store.insert_entity(conn, session_id=sid, kind="decision", confidence="high",
                        payload={}, primary_actor=None, primary_topic="y")
    commits = store.fetch_entities(conn, kind="commitment")
    decisions = store.fetch_entities(conn, kind="decision")
    assert len(commits) == 1
    assert len(decisions) == 1


def test_fetch_entities_filters_by_actor_case_insensitive(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    store.insert_entity(conn, session_id=sid, kind="commitment", confidence="high",
                        payload={}, primary_actor="Qat", primary_topic="x")
    store.insert_entity(conn, session_id=sid, kind="commitment", confidence="high",
                        payload={}, primary_actor="Jason", primary_topic="y")
    out = store.fetch_entities(conn, kind="commitment", primary_actor="qat")
    assert len(out) == 1
    assert out[0]["primary_actor"] == "Qat"


def test_fetch_entities_min_confidence_threshold(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    for conf in ("low", "medium", "high"):
        store.insert_entity(conn, session_id=sid, kind="commitment", confidence=conf,
                            payload={}, primary_actor="A", primary_topic=conf)
    high_only = store.fetch_entities(conn, kind="commitment", min_confidence="high")
    medium_plus = store.fetch_entities(conn, kind="commitment", min_confidence="medium")
    assert len(high_only) == 1
    assert len(medium_plus) == 2


def test_update_entity_status(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    eid = store.insert_entity(conn, session_id=sid, kind="commitment",
                              confidence="high", payload={}, primary_actor="A",
                              primary_topic="x")
    assert store.update_entity_status(conn, eid, "resolved") is True
    # filter by default status='open' should exclude it
    open_only = store.fetch_entities(conn, kind="commitment")
    assert len(open_only) == 0
    # filter all
    all_items = store.fetch_entities(conn, kind="commitment", status=None)
    assert len(all_items) == 1
    assert all_items[0]["status"] == "resolved"


def test_db_stats(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    store.insert_entity(conn, session_id=sid, kind="commitment", confidence="high",
                        payload={}, primary_actor="A", primary_topic="x")
    store.insert_entity(conn, session_id=sid, kind="blocker", confidence="medium",
                        payload={}, primary_actor=None, primary_topic="y")
    stats = store.db_stats(conn)
    assert stats["sessions"] == 1
    assert stats["commitments_open"] == 1
    assert stats["blockers_open"] == 1
    assert stats["decisions_open"] == 0


def test_tx_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    sid = store.insert_session(conn, source_path=None, source_kind="t",
                                model="m", meeting_summary="", participants=[])
    try:
        with store.tx(conn):
            store.insert_entity(
                conn, session_id=sid, kind="commitment", confidence="high",
                payload={}, primary_actor="A", primary_topic="x",
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    rows = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()
    assert rows["n"] == 0
