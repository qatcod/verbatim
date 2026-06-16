"""Tests for v0.12.3 — plain-language simplify mode (verbatim.simplify) and
the shared verbatim.llm completion helper.

LLM calls are mocked at the httpx layer via the Ollama backend; these tests
exercise prompt assembly, entity flattening, and result plumbing — not the
model's actual rewriting.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from verbatim import ask, llm, simplify, state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)


def _diag() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )


def _ollama_handler(reply_text: str, captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": reply_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 12},
        })
    return handler


# ----- llm.complete -----


def test_llm_complete_ollama_returns_text(monkeypatch) -> None:
    captured: dict = {}
    mock_client = httpx.Client(
        transport=httpx.MockTransport(_ollama_handler("plain answer", captured))
    )
    real = llm._complete_ollama

    def fake(model, system, user, *, max_tokens, http_client=None):
        return real(model, system, user, max_tokens=max_tokens,
                    http_client=mock_client)

    monkeypatch.setattr(llm, "_complete_ollama", fake)
    result = llm.complete("SYS", "USER", model="ollama:llama3.1:8b")
    assert result.text == "plain answer"
    assert result.model == "ollama:llama3.1:8b"
    assert result.input_tokens == 50
    assert result.output_tokens == 12
    assert "SYS" in captured["body"]
    assert "USER" in captured["body"]


def test_llm_complete_ollama_empty_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="no choices"):
        llm._complete_ollama(
            "llama3.1:8b", "s", "u", max_tokens=100, http_client=mock_client,
        )


# ----- simplify_text -----


def test_simplify_text_empty_input_short_circuits() -> None:
    result = simplify.simplify_text("")
    assert "Nothing to simplify" in result.text
    # No model call was made.
    assert result.model == "(none)"


def test_simplify_text_calls_model(monkeypatch) -> None:
    captured: dict = {}
    mock_client = httpx.Client(
        transport=httpx.MockTransport(
            _ollama_handler("Here it is in plain words.", captured)
        )
    )
    real = llm._complete_ollama

    def fake(model, system, user, *, max_tokens, http_client=None):
        return real(model, system, user, max_tokens=max_tokens,
                    http_client=mock_client)

    monkeypatch.setattr(llm, "_complete_ollama", fake)
    result = simplify.simplify_text(
        "blocked on the Cyren tier-3 JWT audience binding",
        model="ollama:llama3.1:8b",
    )
    assert result.text == "Here it is in plain words."
    # The jargon text reached the model.
    assert "JWT" in captured["body"]
    # The system prompt enforces the no-jargon contract.
    assert "acronym" in captured["body"].lower()


def test_simplify_text_audience_threaded_into_prompt(monkeypatch) -> None:
    captured: dict = {}
    mock_client = httpx.Client(
        transport=httpx.MockTransport(_ollama_handler("ok", captured))
    )
    real = llm._complete_ollama

    def fake(model, system, user, *, max_tokens, http_client=None):
        return real(model, system, user, max_tokens=max_tokens,
                    http_client=mock_client)

    monkeypatch.setattr(llm, "_complete_ollama", fake)
    simplify.simplify_text(
        "some text", audience="a CFO", model="ollama:llama3.1:8b",
    )
    assert "CFO" in captured["body"]


# ----- entity_to_text -----


def test_entity_to_text_commitment() -> None:
    entity = {
        "kind": "commitment",
        "payload": {
            "actor": "Alice", "deliverable": "ship the prototype",
            "deadline": "Friday",
        },
        "sources": [{"verbatim_quote": "I'll ship it Friday."}],
    }
    text = simplify.entity_to_text(entity)
    assert "Alice" in text
    assert "ship the prototype" in text
    assert "Friday" in text
    assert "I'll ship it Friday." in text


def test_entity_to_text_blocker() -> None:
    entity = {
        "kind": "blocker",
        "payload": {
            "blocked_thing": "launch", "blocked_by": "security review",
            "owner": "Alice",
        },
        "sources": [],
    }
    text = simplify.entity_to_text(entity)
    assert "launch" in text
    assert "security review" in text


# ----- simplify_entity -----


def test_simplify_entity_missing_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    state.open_db(db).close()
    conn = state.open_db(db)
    try:
        result = simplify.simplify_entity(conn, "deadbeef" * 4)
    finally:
        conn.close()
    assert result is None


def test_simplify_entity_flattens_and_simplifies(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "s.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Alice"],
                blockers=[Blocker(
                    blocked_thing="public launch",
                    blocked_by="the Cyren tier-3 JWT audience binding",
                    owner="Alice", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="JWT audience is wrong.",
                        speaker="Alice", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        eid = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    captured: dict = {}
    mock_client = httpx.Client(
        transport=httpx.MockTransport(
            _ollama_handler("The launch is held up by a login-token problem.",
                            captured)
        )
    )
    real = llm._complete_ollama

    def fake(model, system, user, *, max_tokens, http_client=None):
        return real(model, system, user, max_tokens=max_tokens,
                    http_client=mock_client)

    monkeypatch.setattr(llm, "_complete_ollama", fake)
    conn = state.open_db(db)
    try:
        result = simplify.simplify_entity(conn, eid, model="ollama:llama3.1:8b")
    finally:
        conn.close()
    assert result is not None
    assert "login-token" in result.text
    assert "JWT" in captured["body"]  # original jargon flattened in


# ----- ask --simple -----


def test_ask_plain_language_adds_rule(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "a.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Alice"],
                commitments=[Commitment(
                    actor="Alice", deliverable="ship", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
    finally:
        conn.close()

    captured: dict = {}
    mock_client = httpx.Client(
        transport=httpx.MockTransport(_ollama_handler("plain answer", captured))
    )
    real = llm._complete_ollama

    def fake(model, system, user, *, max_tokens, http_client=None):
        return real(model, system, user, max_tokens=max_tokens,
                    http_client=mock_client)

    monkeypatch.setattr(llm, "_complete_ollama", fake)
    conn = state.open_db(db)
    try:
        ask.answer(conn, "what's up?", model="ollama:llama3.1:8b",
                   plain_language=True)
    finally:
        conn.close()
    # The plain-language rule reached the system prompt.
    assert "non-technical reader" in captured["body"]


# ----- web entity-detail ?simple=1 -----


def test_entity_detail_simplify_panel(tmp_path: Path) -> None:
    """?simple=1 renders the plain-language panel. With no model configured
    the simplify call fails gracefully — the panel still shows."""
    from starlette.testclient import TestClient

    from verbatim import web

    db = tmp_path / "w.db"
    conn = state.open_db(db)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Alice"],
                commitments=[Commitment(
                    actor="Alice", deliverable="ship", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        eid = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    client = TestClient(web.create_app(db_path=db))
    # Normal page shows a Simplify button.
    normal = client.get(f"/entity/{eid}")
    assert normal.status_code == 200
    assert f"/entity/{eid}?simple=1" in normal.text
    # ?simple=1 renders the panel (text or graceful fallback).
    simple = client.get(f"/entity/{eid}?simple=1")
    assert simple.status_code == 200
    assert "Plain language" in simple.text
