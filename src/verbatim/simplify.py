"""Plain-language mode — rewrite jargon-heavy content for any reader.

A CEO reads "blocked on the Cyren tier-3 JWT audience binding" and bounces
off it. An engineer reads a finance commitment full of accounting terms and
does the same. `simplify` rewrites either one into plain language: acronyms
expanded, jargon replaced, every fact kept.

It's a one-shot LLM completion over `llm.complete` — no state context, just
the text in and a plainer version out.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import llm, state

SIMPLIFY_SYSTEM_PROMPT = (
    "You rewrite text so anyone can understand it — a non-technical "
    "executive, a new hire, someone outside the domain.\n\n"
    "Rules:\n"
    "- Expand every acronym and abbreviation the first time it appears, "
    "e.g. 'JWT (a kind of secure login token)'.\n"
    "- Replace jargon and technical terms with plain words, or explain them "
    "in a short aside.\n"
    "- Keep every fact, name, number, and deadline exactly as given. Do not "
    "add information that isn't in the original. Do not drop information.\n"
    "- Keep it brief — a few clear sentences. Don't pad.\n"
    "- Write in plain, direct English. No preamble like 'Here is the "
    "simplified version' — just give the rewrite.\n"
)


@dataclass
class SimplifyResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def simplify_text(
    text: str,
    *,
    audience: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 600,
) -> SimplifyResult:
    """Rewrite `text` in plain language.

    `audience` optionally tunes who it's for ("a CFO", "a junior engineer");
    when omitted the prompt targets a general non-technical reader.
    """
    if not text or not text.strip():
        return SimplifyResult(
            text="(Nothing to simplify.)", model="(none)",
            input_tokens=0, output_tokens=0,
        )
    audience_line = (
        f"Rewrite this specifically for: {audience}.\n\n" if audience else ""
    )
    user_message = f"{audience_line}TEXT TO REWRITE:\n{text.strip()}"
    result = llm.complete(
        SIMPLIFY_SYSTEM_PROMPT, user_message,
        model=model, api_key=api_key, max_tokens=max_tokens,
    )
    return SimplifyResult(
        text=result.text,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def entity_to_text(entity: dict) -> str:
    """Flatten an entity into a plain prose block, ready to simplify."""
    payload = entity.get("payload") or {}
    kind = entity["kind"]
    parts: list[str] = []
    if kind == "commitment":
        parts.append(
            f"{payload.get('actor') or 'Someone'} committed to: "
            f"{payload.get('deliverable') or '(unspecified)'}."
        )
        if payload.get("deadline"):
            parts.append(f"Deadline: {payload['deadline']}.")
    elif kind == "decision":
        parts.append(
            f"Decision on {payload.get('topic') or '(a topic)'}: "
            f"{payload.get('outcome') or '(unspecified)'}."
        )
        if payload.get("rationale"):
            parts.append(f"Reasoning: {payload['rationale']}.")
    elif kind == "open_question":
        parts.append(
            f"Open question raised by "
            f"{payload.get('raised_by') or 'someone'}: "
            f"{payload.get('question') or payload.get('topic') or '(unspecified)'}"
        )
    elif kind == "blocker":
        parts.append(
            f"{payload.get('blocked_thing') or 'Something'} is blocked by "
            f"{payload.get('blocked_by') or '(unspecified)'}. "
            f"Owner: {payload.get('owner') or 'unassigned'}."
        )
    sources = entity.get("sources") or []
    if sources:
        quote = (sources[0].get("verbatim_quote") or "").strip()
        if quote:
            parts.append(f'Original words: "{quote}"')
    return " ".join(parts)


def simplify_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    audience: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> SimplifyResult | None:
    """Simplify one entity's content. Returns None if the entity is missing."""
    entity = state.show_entity(conn, entity_id)
    if entity is None:
        return None
    return simplify_text(
        entity_to_text(entity), audience=audience, model=model, api_key=api_key,
    )
