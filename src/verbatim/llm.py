"""Shared plain-text LLM completion — backend-dispatched.

The extractor calls the LLM with forced tool use. Features like `ask` and
`simplify` want the opposite: a free-text answer, no tools. This module is
the one place that does a plain chat completion, dispatching between the
Anthropic API and an Ollama-served model exactly like the extractor does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from anthropic import Anthropic

from .extractor import DEFAULT_MODEL, DEFAULT_OLLAMA_HOST, _is_ollama


@dataclass
class CompletionResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 1024,
) -> CompletionResult:
    """Run one system+user chat completion and return the text answer.

    Model resolution: explicit `model` arg > `$VERBATIM_MODEL` > the
    extractor's `DEFAULT_MODEL`. An `ollama:` prefix (or
    `$VERBATIM_LLM_BACKEND=ollama`) routes to a local model.
    """
    chosen = model or os.environ.get("VERBATIM_MODEL") or DEFAULT_MODEL
    if _is_ollama(chosen):
        return _complete_ollama(
            chosen.removeprefix("ollama:"), system, user, max_tokens=max_tokens,
        )
    return _complete_anthropic(
        chosen, system, user, max_tokens=max_tokens, api_key=api_key,
    )


def _complete_anthropic(
    chosen_model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    api_key: str | None,
) -> CompletionResult:
    client = Anthropic(api_key=api_key) if api_key else Anthropic()
    response = client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        getattr(b, "text", "") for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()
    return CompletionResult(
        text=text or "(The model returned an empty answer.)",
        model=chosen_model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _complete_ollama(
    ollama_model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    http_client: httpx.Client | None = None,
) -> CompletionResult:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    url = f"{host}/v1/chat/completions"
    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
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
    text = (choices[0].get("message", {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    return CompletionResult(
        text=text or "(The model returned an empty answer.)",
        model=f"ollama:{ollama_model}",
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
    )
