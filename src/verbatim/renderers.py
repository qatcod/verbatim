"""Render an ExtractionResult to JSON and human-readable Markdown."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .extractor import ExtractionDiagnostics
from .schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)

_CONFIDENCE_BADGE = {
    Confidence.HIGH: "high confidence",
    Confidence.MEDIUM: "medium confidence",
    Confidence.LOW: "low confidence",
}


def to_json(
    result: ExtractionResult,
    diagnostics: ExtractionDiagnostics | None = None,
    *,
    source_path: str | None = None,
) -> str:
    """Serialize the extraction to a JSON string with metadata block."""
    payload: dict = {
        "schema_version": "0.1.0",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "extraction": result.model_dump(),
    }
    if source_path:
        payload["source_transcript"] = source_path
    if diagnostics:
        payload["diagnostics"] = {
            "model": diagnostics.model,
            "input_tokens": diagnostics.input_tokens,
            "output_tokens": diagnostics.output_tokens,
            "stop_reason": diagnostics.stop_reason,
            "transcript_chars": diagnostics.transcript_chars,
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def to_markdown(
    result: ExtractionResult,
    *,
    source_path: str | None = None,
) -> str:
    """Render the extraction as a human-readable Markdown report."""
    lines: list[str] = []

    title = f"# Meeting summary"
    if source_path:
        title += f" — `{source_path}`"
    lines.append(title)
    lines.append("")
    lines.append(result.meeting_summary.strip())
    lines.append("")

    if result.participants:
        lines.append(f"**Participants:** {', '.join(result.participants)}")
        lines.append("")

    counts = (
        f"{len(result.commitments)} commitments · "
        f"{len(result.decisions)} decisions · "
        f"{len(result.open_questions)} open questions · "
        f"{len(result.blockers)} blockers"
    )
    lines.append(f"_{counts}_")
    lines.append("")

    if result.commitments:
        lines.append("## Commitments")
        lines.append("")
        for c in result.commitments:
            lines.extend(_render_commitment(c))
            lines.append("")

    if result.decisions:
        lines.append("## Decisions")
        lines.append("")
        for d in result.decisions:
            lines.extend(_render_decision(d))
            lines.append("")

    if result.open_questions:
        lines.append("## Open questions")
        lines.append("")
        for q in result.open_questions:
            lines.extend(_render_question(q))
            lines.append("")

    if result.blockers:
        lines.append("## Blockers")
        lines.append("")
        for b in result.blockers:
            lines.extend(_render_blocker(b))
            lines.append("")

    if not (
        result.commitments
        or result.decisions
        or result.open_questions
        or result.blockers
    ):
        lines.append("_No structured items extracted from this transcript._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_commitment(c: Commitment) -> list[str]:
    deadline = f" by **{c.deadline}**" if c.deadline else ""
    to = f" → {c.to}" if c.to else ""
    head = f"- **{c.actor}**{to} — {c.deliverable}{deadline}  _({_CONFIDENCE_BADGE[c.confidence]})_"
    lines = [head]
    if c.notes:
        lines.append(f"  - {c.notes}")
    lines.extend(_render_sources(c.sources, indent="  "))
    return lines


def _render_decision(d: Decision) -> list[str]:
    head = f"- **{d.topic}** → {d.outcome}  _({_CONFIDENCE_BADGE[d.confidence]})_"
    lines = [head]
    if d.participants:
        lines.append(f"  - Participants: {', '.join(d.participants)}")
    if d.rationale:
        lines.append(f"  - Rationale: {d.rationale}")
    if d.alternatives_considered:
        lines.append(f"  - Alternatives considered: {', '.join(d.alternatives_considered)}")
    lines.extend(_render_sources(d.sources, indent="  "))
    return lines


def _render_question(q: OpenQuestion) -> list[str]:
    raised = f" (raised by {q.raised_by}" if q.raised_by else ""
    if q.addressed_to:
        raised += f" → {q.addressed_to}"
    if raised:
        raised += ")"
    urgency = f" _[urgency: {q.urgency}]_" if q.urgency else ""
    head = f"- **{q.topic}**{raised} — {q.question}{urgency}  _({_CONFIDENCE_BADGE[q.confidence]})_"
    lines = [head]
    lines.extend(_render_sources(q.sources, indent="  "))
    return lines


def _render_blocker(b: Blocker) -> list[str]:
    owner = f" (owner: {b.owner})" if b.owner else ""
    head = f"- **{b.blocked_thing}** blocked by **{b.blocked_by}**{owner}  _({_CONFIDENCE_BADGE[b.confidence]})_"
    lines = [head]
    lines.extend(_render_sources(b.sources, indent="  "))
    return lines


def _render_sources(sources: list[SourceReference], *, indent: str) -> list[str]:
    out: list[str] = []
    for s in sources:
        speaker = f"{s.speaker}: " if s.speaker else ""
        timestamp = f"[{s.approximate_timestamp}] " if s.approximate_timestamp else ""
        out.append(f'{indent}> {timestamp}{speaker}{s.verbatim_quote.strip()}')
    return out
