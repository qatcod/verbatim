"""Natural-language query over Verbatim state.

`verbatim ask "what did we decide about the database?"` — instead of picking
the right structured query, ask in plain English. This module assembles the
current open state into a compact context block, hands it to the LLM with the
question, and returns a grounded natural-language answer.

# Grounding contract

The model is instructed to answer ONLY from the provided state and to cite
`VRB-<id>` references. When the state doesn't contain the answer it must say
so rather than inventing one. This mirrors the extractor's verbatim-quote
contract: no source, no claim.

# Backend

Reuses the extractor's Anthropic/Ollama split. Unlike extraction (which forces
a tool call), `ask` wants a free-text answer, so it calls the chat endpoints
without tools.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import httpx
from anthropic import Anthropic

from . import state
from .extractor import DEFAULT_MODEL, DEFAULT_OLLAMA_HOST, _is_ollama

ASK_SYSTEM_PROMPT = (
    "You are Verbatim's query assistant. You answer questions about a "
    "software team's tracked operational state — their commitments, "
    "decisions, open questions, and blockers.\n\n"
    "Rules:\n"
    "- Answer ONLY from the STATE provided in the user message. Do not invent "
    "facts, names, dates, or outcomes.\n"
    "- When you reference an item, cite its `VRB-xxxxxxxx` id.\n"
    "- Quote the verbatim source text when it strengthens the answer.\n"
    "- If the state does not contain the answer, say so plainly — do not "
    "guess.\n"
    "- Be concise. A few sentences is usually enough. Use short bullet lists "
    "for multiple items.\n"
)

# Cap the state block so a huge corpus doesn't blow the context window.
MAX_ENTITIES_IN_CONTEXT = 400


@dataclass
class AskResult:
    answer: str
    model: str
    input_tokens: int
    output_tokens: int
    entities_considered: int


def build_state_context(conn: sqlite3.Connection) -> tuple[str, int]:
    """Serialize current open state into a compact text block for the LLM.

    Returns (context_text, entity_count). Each entity is one block: its
    VRB-id, kind, key fields, and the first verbatim source quote.
    """
    lines: list[str] = []
    count = 0
    sections = [
        ("COMMITMENTS", state.list_commitments(conn, limit=MAX_ENTITIES_IN_CONTEXT)),
        ("DECISIONS", state.list_decisions(conn, limit=MAX_ENTITIES_IN_CONTEXT)),
        ("OPEN QUESTIONS", state.list_open_questions(conn, limit=MAX_ENTITIES_IN_CONTEXT)),
        ("BLOCKERS", state.list_blockers(conn, limit=MAX_ENTITIES_IN_CONTEXT)),
    ]
    for label, items in sections:
        if not items:
            continue
        lines.append(f"## {label}")
        for it in items:
            count += 1
            lines.append(_entity_block(it))
        lines.append("")
    if count == 0:
        return ("(The state graph is empty — nothing has been ingested yet.)", 0)
    return ("\n".join(lines).rstrip(), count)


def _entity_block(entity: dict) -> str:
    """One entity rendered as a compact labelled block."""
    payload = entity.get("payload") or {}
    kind = entity["kind"]
    vrb = f"VRB-{entity['id'][:8]}"
    parts: list[str] = [f"- {vrb} [{kind}]"]
    if kind == "commitment":
        parts.append(f" {payload.get('actor') or '?'}: {payload.get('deliverable') or '?'}")
        if payload.get("deadline"):
            parts.append(f" (deadline: {payload['deadline']})")
    elif kind == "decision":
        parts.append(f" {payload.get('topic') or '?'} -> {payload.get('outcome') or '?'}")
    elif kind == "open_question":
        parts.append(
            f" {payload.get('question') or payload.get('topic') or '?'} "
            f"(raised by {payload.get('raised_by') or '?'})"
        )
    elif kind == "blocker":
        parts.append(
            f" {payload.get('blocked_thing') or '?'} blocked by "
            f"{payload.get('blocked_by') or '?'} (owner: {payload.get('owner') or '?'})"
        )
    line = "".join(parts)
    sources = entity.get("sources") or []
    if sources:
        quote = (sources[0].get("verbatim_quote") or "").strip()
        if quote:
            line += f'\n    quote: "{quote}"'
    return line


def answer(
    conn: sqlite3.Connection,
    question: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 1024,
) -> AskResult:
    """Answer a natural-language question about the current state."""
    chosen_model = model or os.environ.get("VERBATIM_MODEL") or DEFAULT_MODEL
    context, entity_count = build_state_context(conn)
    user_message = (
        f"STATE:\n{context}\n\n"
        f"QUESTION: {question.strip()}\n\n"
        "Answer the question using only the STATE above."
    )
    if _is_ollama(chosen_model):
        return _answer_ollama(
            chosen_model.removeprefix("ollama:"), user_message,
            entity_count=entity_count, max_tokens=max_tokens,
        )
    return _answer_anthropic(
        chosen_model, user_message, entity_count=entity_count,
        max_tokens=max_tokens, api_key=api_key,
    )


def _answer_anthropic(
    chosen_model: str,
    user_message: str,
    *,
    entity_count: int,
    max_tokens: int,
    api_key: str | None,
) -> AskResult:
    client = Anthropic(api_key=api_key) if api_key else Anthropic()
    response = client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=ASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(
        getattr(b, "text", "") for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()
    return AskResult(
        answer=text or "(The model returned an empty answer.)",
        model=chosen_model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        entities_considered=entity_count,
    )


def _answer_ollama(
    ollama_model: str,
    user_message: str,
    *,
    entity_count: int,
    max_tokens: int,
    http_client: httpx.Client | None = None,
) -> AskResult:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    url = f"{host}/v1/chat/completions"
    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": ASK_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
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
    return AskResult(
        answer=text or "(The model returned an empty answer.)",
        model=f"ollama:{ollama_model}",
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        entities_considered=entity_count,
    )
