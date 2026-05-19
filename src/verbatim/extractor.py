"""LLM extraction with pluggable backends.

Two backends today:

- **Anthropic** (default) — Claude API via the `anthropic` SDK. Forced tool use
  yields guaranteed structured output.
- **Ollama** — local LLM via Ollama's OpenAI-compatible chat completions
  endpoint at `http://localhost:11434/v1`. Same tool-use shape, different
  transport. Free, private, no API keys.

# Backend selection

The backend is chosen from the model name + env, in this order:

1. `model.startswith("ollama:")` → Ollama. Strip the prefix to get the
   underlying model (e.g. `ollama:llama3.1:8b` → `llama3.1:8b`).
2. `$VERBATIM_LLM_BACKEND=ollama` → Ollama. Uses `$VERBATIM_MODEL` directly.
3. Otherwise → Anthropic. `$VERBATIM_MODEL` or the function arg picks the
   Claude model.

Ollama host is `$OLLAMA_HOST` or `http://localhost:11434`.

# Tool-use note for Ollama

Not every Ollama model supports tool calling well. Known-good as of 2026-05:
`llama3.1:8b`, `llama3.1:70b`, `qwen2.5:7b`, `qwen2.5:14b`,
`mistral-small3.1:24b`. If extraction fails, try a different model or check
the model card for tool support.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx
from anthropic import Anthropic

from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .schema import ExtractionResult

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


@dataclass
class ExtractionDiagnostics:
    """Metadata about the extraction run — useful for debugging quality."""

    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    transcript_chars: int


def extract(
    transcript: str,
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    api_key: str | None = None,
) -> tuple[ExtractionResult, ExtractionDiagnostics]:
    """Run the extraction LLM call and return the structured result + diagnostics.

    Backend is picked by model prefix or $VERBATIM_LLM_BACKEND. See module
    docstring for the full selection logic.
    """
    chosen_model = model or os.environ.get("VERBATIM_MODEL") or DEFAULT_MODEL

    if _is_ollama(chosen_model):
        ollama_model = chosen_model.removeprefix("ollama:")
        return _extract_ollama(transcript, ollama_model, max_tokens=max_tokens)
    return _extract_anthropic(transcript, chosen_model, max_tokens=max_tokens, api_key=api_key)


def _is_ollama(model: str) -> bool:
    if model.startswith("ollama:"):
        return True
    return os.environ.get("VERBATIM_LLM_BACKEND", "").lower() == "ollama"


def _build_tool_schema_anthropic() -> dict:
    """Anthropic tool definition. `input_schema` field name is theirs."""
    return {
        "name": "extract_meeting_entities",
        "description": (
            "Emit the structured extraction result for the meeting transcript. "
            "Every commitment/decision/question/blocker must include at least one "
            "verbatim quote from the transcript that supports it."
        ),
        "input_schema": ExtractionResult.model_json_schema(),
    }


def _build_tool_schema_openai() -> dict:
    """OpenAI-compat tool definition (used by Ollama). `parameters` field name."""
    return {
        "type": "function",
        "function": {
            "name": "extract_meeting_entities",
            "description": (
                "Emit the structured extraction result for the meeting transcript. "
                "Every commitment/decision/question/blocker must include at least one "
                "verbatim quote from the transcript that supports it."
            ),
            "parameters": ExtractionResult.model_json_schema(),
        },
    }


# Back-compat alias for any external callers / tests.
_build_tool_schema = _build_tool_schema_anthropic


# ----------------------- backends -----------------------


def _extract_anthropic(
    transcript: str,
    chosen_model: str,
    *,
    max_tokens: int,
    api_key: str | None,
) -> tuple[ExtractionResult, ExtractionDiagnostics]:
    client = Anthropic(api_key=api_key) if api_key else Anthropic()
    response = client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        tools=[_build_tool_schema_anthropic()],
        tool_choice={"type": "tool", "name": "extract_meeting_entities"},
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(transcript=transcript),
            }
        ],
    )

    tool_use_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use_block is None:
        raise RuntimeError(
            "Model did not call the extraction tool. Stop reason: "
            f"{response.stop_reason}. Content: {response.content!r}"
        )

    result = ExtractionResult.model_validate(tool_use_block.input)
    diagnostics = ExtractionDiagnostics(
        model=chosen_model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason or "",
        transcript_chars=len(transcript),
    )
    return result, diagnostics


def _extract_ollama(
    transcript: str,
    ollama_model: str,
    *,
    max_tokens: int,
    http_client: httpx.Client | None = None,
) -> tuple[ExtractionResult, ExtractionDiagnostics]:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    url = f"{host}/v1/chat/completions"

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(transcript=transcript),
            },
        ],
        "tools": [_build_tool_schema_openai()],
        "tool_choice": {
            "type": "function",
            "function": {"name": "extract_meeting_entities"},
        },
        "max_tokens": max_tokens,
    }

    owned = http_client is None
    client = http_client or httpx.Client(timeout=300.0)
    try:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owned:
            client.close()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Ollama response had no choices: {data!r}")
    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError(
            f"Ollama model didn't call the extraction tool. "
            f"finish_reason: {choices[0].get('finish_reason')}. "
            f"Model: {ollama_model}. Try a model with stronger tool support "
            "(llama3.1, qwen2.5, mistral-small3.1)."
        )
    args = tool_calls[0].get("function", {}).get("arguments", "{}")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Ollama tool arguments aren't valid JSON: {e}") from e

    result = ExtractionResult.model_validate(args)

    usage = data.get("usage") or {}
    diagnostics = ExtractionDiagnostics(
        model=f"ollama:{ollama_model}",
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        stop_reason=choices[0].get("finish_reason", ""),
        transcript_chars=len(transcript),
    )
    return result, diagnostics
