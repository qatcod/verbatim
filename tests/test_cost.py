"""Cost estimation + spend tracking tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from verbatim import cost, state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)


def test_estimate_cost_sonnet() -> None:
    # 4400 input + 3489 output at 3.0 / 15.0
    # = 4400 * 3 / 1M + 3489 * 15 / 1M = 0.0132 + 0.052335 = 0.065535
    c = cost.estimate_cost("claude-sonnet-4-6", 4400, 3489)
    assert abs(c - 0.065535) < 1e-6


def test_estimate_cost_opus_is_more_expensive_than_sonnet() -> None:
    sonnet = cost.estimate_cost("claude-sonnet-4-6", 1000, 1000)
    opus = cost.estimate_cost("claude-opus-4-7", 1000, 1000)
    assert opus > sonnet * 4  # ~5x more


def test_estimate_cost_unknown_model_returns_zero() -> None:
    assert cost.estimate_cost("claude-imaginary", 10000, 10000) == 0.0


def test_estimate_cost_handles_none_model() -> None:
    assert cost.estimate_cost(None, 100, 100) == 0.0


def test_pricing_override_via_env(monkeypatch) -> None:
    monkeypatch.setenv("VERBATIM_PRICING", "claude-sonnet-4-6:2.5/12")
    import importlib
    importlib.reload(cost)
    try:
        # 1000 in + 1000 out at 2.5 / 12 = 0.0025 + 0.012 = 0.0145
        c = cost.estimate_cost("claude-sonnet-4-6", 1000, 1000)
        assert abs(c - 0.0145) < 1e-6
    finally:
        monkeypatch.delenv("VERBATIM_PRICING")
        importlib.reload(cost)


def _save_session(conn: sqlite3.Connection, *, model: str, in_tokens: int, out_tokens: int) -> None:
    result = ExtractionResult(
        meeting_summary="seed", participants=[],
        commitments=[Commitment(
            actor="A", deliverable="x", confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="q", speaker="A", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model=model, input_tokens=in_tokens, output_tokens=out_tokens,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path=None)


def test_total_spend_sums_across_sessions(tmp_path: Path) -> None:
    db = tmp_path / "spend.db"
    conn = state.open_db(db)
    try:
        _save_session(conn, model="claude-sonnet-4-6", in_tokens=1000, out_tokens=1000)
        _save_session(conn, model="claude-sonnet-4-6", in_tokens=2000, out_tokens=500)
        # 1000*3/1M + 1000*15/1M = 0.003 + 0.015 = 0.018
        # 2000*3/1M + 500*15/1M = 0.006 + 0.0075 = 0.0135
        # Total: 0.0315
        total = cost.total_spend(conn)
        assert abs(total - 0.0315) < 1e-6
    finally:
        conn.close()


def test_spend_by_model_breakdown(tmp_path: Path) -> None:
    db = tmp_path / "by_model.db"
    conn = state.open_db(db)
    try:
        _save_session(conn, model="claude-sonnet-4-6", in_tokens=1000, out_tokens=1000)
        _save_session(conn, model="claude-haiku-4-5", in_tokens=10000, out_tokens=5000)
        breakdown = cost.spend_by_model(conn)
        assert "claude-sonnet-4-6" in breakdown
        assert "claude-haiku-4-5" in breakdown
        # Sonnet: 1000*3/1M + 1000*15/1M = 0.018
        assert abs(breakdown["claude-sonnet-4-6"] - 0.018) < 1e-6
        # Haiku: 10000*1/1M + 5000*5/1M = 0.01 + 0.025 = 0.035
        assert abs(breakdown["claude-haiku-4-5"] - 0.035) < 1e-6
    finally:
        conn.close()


def test_total_tokens(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    conn = state.open_db(db)
    try:
        _save_session(conn, model="claude-sonnet-4-6", in_tokens=1000, out_tokens=500)
        _save_session(conn, model="claude-sonnet-4-6", in_tokens=2000, out_tokens=800)
        in_tok, out_tok = cost.total_tokens(conn)
        assert in_tok == 3000
        assert out_tok == 1300
    finally:
        conn.close()


def test_empty_db_total_spend_is_zero(tmp_path: Path) -> None:
    db = tmp_path / "empty_spend.db"
    conn = state.open_db(db)
    try:
        assert cost.total_spend(conn) == 0.0
        assert cost.total_tokens(conn) == (0, 0)
    finally:
        conn.close()
