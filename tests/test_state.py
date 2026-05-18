"""State layer tests — domain operations on top of the store."""
from __future__ import annotations

import sqlite3

from verbatim import state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import ExtractionResult


def test_save_extraction_persists_everything(
    conn: sqlite3.Connection,
    sample_extraction: ExtractionResult,
    sample_diagnostics: ExtractionDiagnostics,
) -> None:
    summary = state.save_extraction(
        conn, sample_extraction, sample_diagnostics, source_path="t.txt"
    )
    assert summary.counts["commitment"] == 2
    assert summary.counts["decision"] == 1
    assert summary.counts["open_question"] == 1
    assert summary.counts["blocker"] == 1
    sessions = state.recent_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0]["source_path"] == "t.txt"


def test_list_commitments_returns_all_open(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    items = state.list_commitments(conn)
    assert len(items) == 2
    actors = {i["primary_actor"] for i in items}
    assert actors == {"Qat", "Taz"}


def test_list_commitments_filters_by_actor(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    items = state.list_commitments(conn, actor="qat")
    assert len(items) == 1
    assert items[0]["primary_actor"] == "Qat"


def test_list_commitments_filters_by_min_confidence(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    high_only = state.list_commitments(conn, min_confidence="high")
    assert len(high_only) == 1
    assert high_only[0]["confidence"] == "high"


def test_resolve_entity_hides_from_default_query(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    items = state.list_commitments(conn)
    target = items[0]
    assert state.resolve_entity(conn, target["id"]) is True
    after = state.list_commitments(conn)
    assert len(after) == len(items) - 1


def test_payload_preserves_kind_specific_fields(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    commits = state.list_commitments(conn, actor="Qat")
    payload = commits[0]["payload"]
    assert payload["actor"] == "Qat"
    assert payload["deliverable"] == "v0 of Verbatim CLI"
    assert payload["deadline"] == "EOD Wednesday"


def test_sources_round_trip(
    conn, sample_extraction, sample_diagnostics
) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    items = state.list_commitments(conn, actor="Qat")
    sources = items[0]["sources"]
    assert len(sources) == 1
    assert sources[0]["verbatim_quote"] == (
        "I'll have a working version by end of day Wednesday."
    )
    assert sources[0]["speaker"] == "Qat"


def test_stats(conn, sample_extraction, sample_diagnostics) -> None:
    state.save_extraction(conn, sample_extraction, sample_diagnostics, source_path=None)
    s = state.stats(conn)
    assert s["sessions"] == 1
    assert s["commitments_open"] == 2
    assert s["decisions_open"] == 1
    assert s["open_questions_open"] == 1
    assert s["blockers_open"] == 1
