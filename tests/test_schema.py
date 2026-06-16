"""Schema (Pydantic model) tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)


def _ref() -> SourceReference:
    return SourceReference(
        verbatim_quote="test quote",
        speaker="Alice",
        rationale="because.",
    )


def test_commitment_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        Commitment(
            actor="X", deliverable="Y", confidence=Confidence.HIGH, sources=[]
        )


def test_decision_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        Decision(topic="t", outcome="o", confidence=Confidence.HIGH, sources=[])


def test_open_question_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        OpenQuestion(
            topic="t", question="?", confidence=Confidence.HIGH, sources=[]
        )


def test_blocker_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        Blocker(
            blocked_thing="x", blocked_by="y", confidence=Confidence.HIGH, sources=[]
        )


def test_confidence_enum_values() -> None:
    assert Confidence.HIGH.value == "high"
    assert Confidence.MEDIUM.value == "medium"
    assert Confidence.LOW.value == "low"


def test_extraction_result_round_trip() -> None:
    original = ExtractionResult(
        meeting_summary="test",
        participants=["A", "B"],
        commitments=[
            Commitment(
                actor="A", deliverable="thing",
                confidence=Confidence.HIGH, sources=[_ref()],
            )
        ],
    )
    raw = original.model_dump()
    reloaded = ExtractionResult.model_validate(raw)
    assert reloaded.commitments[0].actor == "A"
    assert reloaded.commitments[0].sources[0].verbatim_quote == "test quote"


def test_optional_fields_default_to_none_or_empty() -> None:
    c = Commitment(
        actor="A", deliverable="thing",
        confidence=Confidence.HIGH, sources=[_ref()],
    )
    assert c.deadline is None
    assert c.to is None
    assert c.notes is None


def test_extraction_result_empty_lists_default() -> None:
    r = ExtractionResult(meeting_summary="x")
    assert r.commitments == []
    assert r.decisions == []
    assert r.open_questions == []
    assert r.blockers == []
    assert r.participants == []


def test_json_schema_emits_required_quote_field() -> None:
    schema = ExtractionResult.model_json_schema()
    # SourceReference is a $def
    src_schema = schema["$defs"]["SourceReference"]
    assert "verbatim_quote" in src_schema["required"]
    assert "rationale" in src_schema["required"]
