"""Tests for v0.12.0 — natural-language ask (verbatim.ask).

The LLM call itself is mocked; these tests exercise state-context assembly,
backend dispatch, and the AskResult shape — not Claude's reasoning.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from verbatim import ask, state
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


def _diag() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )


def _seed(db_path: Path) -> None:
    conn = state.open_db(db_path)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat", "Jason"],
                commitments=[Commitment(
                    actor="Qat", deliverable="ship the CULA prototype",
                    deadline="Friday", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="I'll ship CULA by Friday.",
                        speaker="Qat", rationale="r")],
                )],
                decisions=[Decision(
                    topic="database choice", outcome="use Postgres",
                    participants=["Qat", "Jason"], confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="Postgres it is.",
                        speaker="Jason", rationale="r")],
                )],
                open_questions=[OpenQuestion(
                    topic="ops", question="who owns the m2w config?",
                    raised_by="Jason", confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="who owns m2w?",
                        speaker="Jason", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="launch", blocked_by="security review",
                    owner="Qat", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="security first.",
                        speaker="Qat", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
    finally:
        conn.close()


# ----- state context assembly -----


def test_build_state_context_includes_all_kinds(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db)
    conn = state.open_db(db)
    try:
        context, count = ask.build_state_context(conn)
    finally:
        conn.close()
    assert count == 4
    assert "COMMITMENTS" in context
    assert "DECISIONS" in context
    assert "OPEN QUESTIONS" in context
    assert "BLOCKERS" in context
    assert "ship the CULA prototype" in context
    assert "use Postgres" in context


def test_build_state_context_includes_quotes(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db)
    conn = state.open_db(db)
    try:
        context, _ = ask.build_state_context(conn)
    finally:
        conn.close()
    assert "I'll ship CULA by Friday." in context


def test_build_state_context_uses_vrb_ids(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db)
    conn = state.open_db(db)
    try:
        context, _ = ask.build_state_context(conn)
    finally:
        conn.close()
    assert "VRB-" in context


def test_build_state_context_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    state.open_db(db).close()
    conn = state.open_db(db)
    try:
        context, count = ask.build_state_context(conn)
    finally:
        conn.close()
    assert count == 0
    assert "empty" in context.lower()


# ----- backend dispatch (Ollama path, mocked HTTP) -----


def test_answer_ollama_path(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "a.db"
    _seed(db)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": "Qat is shipping CULA by Friday (VRB-...)."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 18},
        })

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    real_answer_ollama = ask._answer_ollama

    def fake_answer_ollama(model, user_message, *, entity_count, max_tokens,
                           http_client=None):
        return real_answer_ollama(
            model, user_message, entity_count=entity_count,
            max_tokens=max_tokens, http_client=mock_client,
        )

    monkeypatch.setattr(ask, "_answer_ollama", fake_answer_ollama)

    conn = state.open_db(db)
    try:
        result = ask.answer(conn, "what is Qat doing?", model="ollama:llama3.1:8b")
    finally:
        conn.close()
    assert "CULA" in result.answer
    assert result.model == "ollama:llama3.1:8b"
    assert result.input_tokens == 120
    assert result.output_tokens == 18
    assert result.entities_considered == 4
    # The question + state both reached the model.
    assert "what is Qat doing?" in captured["body"]
    assert "Postgres" in captured["body"]


def test_answer_ollama_empty_choices_raises(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    import pytest
    with pytest.raises(RuntimeError, match="no choices"):
        ask._answer_ollama(
            "llama3.1:8b", "STATE\n\nQUESTION: x",
            entity_count=0, max_tokens=512, http_client=mock_client,
        )


def test_ask_system_prompt_grounding_rules() -> None:
    """The system prompt must enforce the grounding contract."""
    p = ask.ASK_SYSTEM_PROMPT.lower()
    assert "only from the state" in p
    assert "do not" in p  # no-invention rule
    assert "vrb-" in p     # citation rule
