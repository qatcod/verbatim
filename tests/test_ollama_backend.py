"""Tests for the Ollama LLM backend in extractor.py.

All HTTP traffic is mocked via httpx.MockTransport. No real network or local
Ollama server is required.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from verbatim import extractor

# ----- backend selection -----


def test_is_ollama_via_model_prefix() -> None:
    assert extractor._is_ollama("ollama:llama3.1:8b") is True


def test_is_ollama_via_env_var(monkeypatch) -> None:
    monkeypatch.setenv("VERBATIM_LLM_BACKEND", "ollama")
    assert extractor._is_ollama("anything") is True


def test_is_ollama_default_false(monkeypatch) -> None:
    monkeypatch.delenv("VERBATIM_LLM_BACKEND", raising=False)
    assert extractor._is_ollama("claude-sonnet-4-6") is False


# ----- ollama extraction happy path -----


def _good_tool_call_response() -> dict[str, Any]:
    """Build a minimal OpenAI-compat response that contains a valid extraction."""
    args = {
        "meeting_summary": "Quick sync.",
        "participants": ["Qat", "Jason"],
        "commitments": [{
            "actor": "Qat",
            "deliverable": "ship v0",
            "deadline": "Friday",
            "confidence": "high",
            "sources": [{
                "verbatim_quote": "I'll ship Friday.",
                "speaker": "Qat",
                "rationale": "explicit",
            }],
        }],
        "decisions": [],
        "open_questions": [],
        "blockers": [],
    }
    return {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_test",
                    "type": "function",
                    "function": {
                        "name": "extract_meeting_entities",
                        "arguments": json.dumps(args),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 400},
    }


def test_extract_ollama_happy_path(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "url": str(request.url),
            "method": request.method,
            "body": json.loads(request.content),
        })
        return httpx.Response(200, json=_good_tool_call_response())

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    monkeypatch.setenv("OLLAMA_HOST", "http://test.local:11434")
    result, diag = extractor._extract_ollama(
        "Qat: I'll ship Friday.",
        "llama3.1:8b",
        max_tokens=4096,
        http_client=client,
    )

    assert len(result.commitments) == 1
    assert result.commitments[0].actor == "Qat"
    assert result.commitments[0].sources[0].verbatim_quote == "I'll ship Friday."

    # Diagnostics carry the model prefix and the right token usage
    assert diag.model == "ollama:llama3.1:8b"
    assert diag.input_tokens == 1500
    assert diag.output_tokens == 400

    # Request payload uses the OpenAI-compat tool format
    assert captured[0]["url"] == "http://test.local:11434/v1/chat/completions"
    body = captured[0]["body"]
    assert body["model"] == "llama3.1:8b"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "extract_meeting_entities"
    assert body["tool_choice"]["type"] == "function"


def test_extract_dispatches_to_ollama_for_ollama_model(monkeypatch) -> None:
    """extract(model='ollama:...') must route through _extract_ollama."""
    from verbatim import extractor as ext_module

    calls: list[tuple] = []

    def fake_ollama(transcript, ollama_model, *, max_tokens, http_client=None):
        calls.append((transcript[:20], ollama_model, max_tokens))
        # Return a minimal valid result so the test reaches the assertion below
        from verbatim.schema import ExtractionResult
        return ExtractionResult(meeting_summary="x"), ExtractionDiagnosticsStub(
            model=f"ollama:{ollama_model}",
        )

    class ExtractionDiagnosticsStub:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.input_tokens = 0
            self.output_tokens = 0
            self.stop_reason = ""
            self.transcript_chars = 0

    monkeypatch.setattr(ext_module, "_extract_ollama", fake_ollama)
    result, diag = ext_module.extract(
        "Qat: I'll ship Friday.",
        model="ollama:qwen2.5:7b",
    )
    assert len(calls) == 1
    assert calls[0][1] == "qwen2.5:7b"
    assert diag.model == "ollama:qwen2.5:7b"


def test_extract_dispatches_to_anthropic_for_default_model(monkeypatch) -> None:
    """extract() without model arg routes through _extract_anthropic."""
    from verbatim import extractor as ext_module

    calls: list[tuple] = []

    def fake_anthropic(transcript, chosen_model, *, max_tokens, api_key):
        calls.append((chosen_model, max_tokens))
        from verbatim.schema import ExtractionResult
        return ExtractionResult(meeting_summary="x"), object()

    monkeypatch.setattr(ext_module, "_extract_anthropic", fake_anthropic)
    monkeypatch.delenv("VERBATIM_LLM_BACKEND", raising=False)
    monkeypatch.delenv("VERBATIM_MODEL", raising=False)
    ext_module.extract("hi")
    assert calls[0][0] == "claude-sonnet-4-6"


# ----- ollama error paths -----


def test_extract_ollama_raises_when_no_tool_call() -> None:
    """If the model returns prose instead of calling the tool, raise clearly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": "Sorry, I can't extract.", "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(RuntimeError) as exc:
        extractor._extract_ollama(
            "hello",
            "llama3.1:8b",
            max_tokens=4096,
            http_client=client,
        )
    msg = str(exc.value)
    assert "didn't call the extraction tool" in msg
    assert "tool support" in msg.lower()


def test_extract_ollama_raises_on_bad_json_args() -> None:
    """If the tool 'arguments' string isn't valid JSON, raise clearly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "x",
                        "type": "function",
                        "function": {
                            "name": "extract_meeting_entities",
                            "arguments": "{not-json",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(RuntimeError) as exc:
        extractor._extract_ollama(
            "hello",
            "llama3.1:8b",
            max_tokens=4096,
            http_client=client,
        )
    assert "valid JSON" in str(exc.value)


def test_extract_ollama_handles_already_parsed_args() -> None:
    """Some Ollama responses return arguments as a dict already, not a JSON string."""
    args = {
        "meeting_summary": "x",
        "participants": [],
        "commitments": [], "decisions": [], "open_questions": [], "blockers": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "x", "type": "function",
                        "function": {
                            "name": "extract_meeting_entities",
                            "arguments": args,  # dict, not str
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    result, _ = extractor._extract_ollama(
        "hello",
        "llama3.1:8b",
        max_tokens=4096,
        http_client=client,
    )
    assert result.meeting_summary == "x"


def test_extract_ollama_raises_when_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model not loaded"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        extractor._extract_ollama(
            "hello",
            "missing-model",
            max_tokens=4096,
            http_client=client,
        )


# ----- ollama integrates with cost.py (zero cost for local) -----


def test_ollama_models_have_zero_cost_by_default() -> None:
    """Local Ollama runs free — no entry in DEFAULT_PRICING returns $0."""
    from verbatim import cost
    assert cost.estimate_cost("ollama:llama3.1:8b", 1_000_000, 1_000_000) == 0.0
