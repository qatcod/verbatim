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
        "participants": ["Alice", "Bob"],
        "commitments": [{
            "actor": "Alice",
            "deliverable": "ship v0",
            "deadline": "Friday",
            "confidence": "high",
            "sources": [{
                "verbatim_quote": "I'll ship Friday.",
                "speaker": "Alice",
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
        "Alice: I'll ship Friday.",
        "llama3.1:8b",
        max_tokens=4096,
        http_client=client,
    )

    assert len(result.commitments) == 1
    assert result.commitments[0].actor == "Alice"
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
        "Alice: I'll ship Friday.",
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


# ----- lenient validation reaches the ollama path -----
#
# WHY THIS IS HERE. test_schema.py proves validate_extraction_leniently behaves; it cannot prove the
# ollama backend CALLS it. Mutation-testing found exactly that hole: reverting this path to
# `ExtractionResult.model_validate(args)` left all of test_schema.py green, which is a tolerance
# feature that could be silently removed. This is the test that notices.


def _mixed_quality_tool_call_response() -> dict[str, Any]:
    """One good commitment, one unusable one, and a decision with no outcome.

    This is a real response shape, not an invented one: across five runs of qwen2.5:7b on one
    standup transcript, the model omitted `deliverable`, omitted `decisions[0].topic`, and nested
    `confidence` inside an object named after itself. Before per-item validation, any ONE of those
    discarded the whole extraction including the good commitment.
    """
    args = {
        "meeting_summary": "Standup.",
        "participants": ["Priya", "Dan"],
        "commitments": [
            {
                "actor": "Priya",
                "deliverable": "chase Ops for credentials",
                "deadline": "end of day",
                # Nested, as the model really returned it.
                "confidence": {"confidence": "HIGH"},
                "sources": [{"verbatim_quote": "I'll chase Ops this afternoon"}],
            },
            # No deliverable: genuinely not an item, and must be dropped rather than guessed at.
            {"actor": "Dan", "confidence": "high", "sources": [{"verbatim_quote": "Still blocked."}]},
        ],
        # No outcome: unusable. But its presence must not cost Priya's commitment.
        "decisions": [{"topic": "postgres 16", "confidence": "high",
                       "sources": [{"verbatim_quote": "Let's park it"}]}],
        # No topic, which IS tolerated -- `question` carries the substance.
        "open_questions": [{"question": "do we move to pg16 this quarter?", "confidence": "medium",
                            "sources": [{"verbatim_quote": "or has that slipped?"}]}],
        "blockers": [],
    }
    return {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_mixed",
                    "type": "function",
                    "function": {"name": "extract_meeting_entities", "arguments": json.dumps(args)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def test_ollama_keeps_the_good_items_and_drops_only_the_bad(monkeypatch, capsys) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_mixed_quality_tool_call_response()))
    client = httpx.Client(transport=transport)

    result, diag = extractor._extract_ollama(
        "Priya: I'll chase Ops this afternoon. Dan: Still blocked.",
        "qwen2.5:7b", max_tokens=4096, http_client=client,
    )

    # The good commitment survives — the entire point. It used to be discarded with the bad decision.
    assert len(result.commitments) == 1
    assert result.commitments[0].actor == "Priya"
    assert result.commitments[0].deliverable == "chase Ops for credentials"
    # The nested confidence was unwrapped rather than costing the item.
    assert result.commitments[0].confidence.value == "high"
    # The unusable ones are gone.
    assert result.decisions == []
    # And the topic-less question is KEPT, because `question` is the substance.
    assert len(result.open_questions) == 1
    assert result.open_questions[0].topic == ""
    assert diag.model == "ollama:qwen2.5:7b"

    # The drops are announced on stderr. A silent partial extraction looks identical to a meeting
    # with fewer commitments in it.
    err = capsys.readouterr().err
    assert "dropped as malformed" in err
    assert "commitments[1]" in err
    assert "decisions[0]" in err


def test_ollama_still_fails_hard_on_a_reply_that_is_not_an_extraction(monkeypatch) -> None:
    # Leniency stops at the meeting level. No summary means the reply was not an extraction, and
    # treating it as a partial success would ground a thread on an empty document.
    bad = _mixed_quality_tool_call_response()
    args = json.loads(bad["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    args["meeting_summary"] = ""
    bad["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(args)

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=bad))
    client = httpx.Client(transport=transport)
    with pytest.raises(ValueError):
        extractor._extract_ollama("x", "qwen2.5:7b", max_tokens=4096, http_client=client)
