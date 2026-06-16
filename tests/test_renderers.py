"""Renderer tests — JSON and Markdown output structure."""
from __future__ import annotations

import json

from verbatim.extractor import ExtractionDiagnostics
from verbatim.renderers import to_json, to_markdown
from verbatim.schema import ExtractionResult


def test_to_json_round_trips(
    sample_extraction: ExtractionResult,
    sample_diagnostics: ExtractionDiagnostics,
) -> None:
    out = to_json(sample_extraction, sample_diagnostics, source_path="t.txt")
    payload = json.loads(out)
    assert payload["source_transcript"] == "t.txt"
    assert payload["schema_version"] == "0.1.0"
    assert "extracted_at" in payload
    assert payload["diagnostics"]["model"] == "claude-sonnet-4-6"
    assert payload["extraction"]["meeting_summary"].startswith("Kickoff")


def test_to_json_without_diagnostics(sample_extraction: ExtractionResult) -> None:
    out = to_json(sample_extraction)
    payload = json.loads(out)
    assert "diagnostics" not in payload


def test_to_markdown_has_all_sections(sample_extraction: ExtractionResult) -> None:
    md = to_markdown(sample_extraction)
    assert "# Meeting summary" in md
    assert "## Commitments" in md
    assert "## Decisions" in md
    assert "## Open questions" in md
    assert "## Blockers" in md


def test_to_markdown_includes_verbatim_quotes(sample_extraction: ExtractionResult) -> None:
    md = to_markdown(sample_extraction)
    # Every source quote should appear in the markdown body
    for c in sample_extraction.commitments:
        for s in c.sources:
            assert s.verbatim_quote in md


def test_to_markdown_includes_confidence_labels(sample_extraction: ExtractionResult) -> None:
    md = to_markdown(sample_extraction)
    assert "high confidence" in md
    assert "medium confidence" in md


def test_to_markdown_empty_extraction_states_no_items() -> None:
    empty = ExtractionResult(meeting_summary="quick sync, nothing to track.")
    md = to_markdown(empty)
    assert "No structured items extracted" in md


def test_to_markdown_includes_participants(sample_extraction: ExtractionResult) -> None:
    md = to_markdown(sample_extraction)
    assert "**Participants:**" in md
    assert "Alice" in md
    assert "Bob" in md


def test_to_markdown_decision_renders_alternatives(sample_extraction: ExtractionResult) -> None:
    md = to_markdown(sample_extraction)
    # the sample has a "Python" decision with "TypeScript" as alternative
    assert "Alternatives considered: TypeScript" in md
