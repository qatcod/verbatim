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
    validate_extraction_leniently,
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
    # The quote is required and must STAY required: it is the load-bearing artefact. Quote
    # verification checks it is a real substring of the transcript, which is what stops a fabricated
    # action item being posted to a team. Nothing else in this model carries that weight.
    assert "verbatim_quote" in src_schema["required"]


def test_the_rationale_is_not_required() -> None:
    """Deliberately optional, so one missing sentence cannot discard a whole extraction.

    This asserted the opposite until a real run showed what it cost. qwen2.5:7b on a real standup
    transcript produced correct content with verifiable quotes and was rejected with ten validation
    errors -- five of them a missing `rationale`. Ollama's OpenAI-compatible tool calling does not
    grammar-constrain output to the schema, so a local model omits fields a strong model fills in,
    and on-prem Gather runs against whatever local model the customer has.

    Every consumer already guards on truthiness (renderers.py, simplify.py, cli.py, slack_bot.py,
    linear.py, github_issues.py) and Decision's own rationale is already optional, so requiring it
    here was the outlier rather than the rule.
    """
    schema = ExtractionResult.model_json_schema()
    src_schema = schema["$defs"]["SourceReference"]
    assert "rationale" not in src_schema.get("required", [])
    # And a source built without one is valid, defaulting to empty rather than null -- store.py's
    # column is `rationale TEXT NOT NULL`.
    src = SourceReference(verbatim_quote="I will ship it")
    assert src.rationale == ""


def test_confidence_accepts_any_casing() -> None:
    """A local model returns "HIGH"; the schema enumerates "high". Case is not meaning.

    Five of the ten validation errors in that same real run were this. Rejecting the extraction over
    the spelling of three known words discarded every commitment, decision, question and blocker in
    the meeting.
    """
    for raw in ("high", "HIGH", "High", " high ", "hIgH"):
        assert Confidence(raw) is Confidence.HIGH, raw
    assert Confidence("MEDIUM") is Confidence.MEDIUM
    assert Confidence("Low") is Confidence.LOW
    # The vocabulary stays CLOSED -- this widens the accepted spelling of three values, not the set.
    for bad in ("very high", "certain", "", "unknown", "1"):
        with pytest.raises(ValueError):
            Confidence(bad)


# ----------------------- lenient batch / strict item -----------------------
#
# Why this exists at all, in one line: `ExtractionResult.model_validate` is all-or-nothing, so one
# malformed field anywhere discarded every commitment, decision, question and blocker in the
# transcript. Measured against qwen2.5:7b on a real standup: 2 of 5 runs validated, and in the three
# that failed the commitments were fine and were thrown away with everything else.


def _src(q: str = "I will ship the export") -> dict:
    return {"verbatim_quote": q}


def _ok_commitment(deliverable: str = "ship the export") -> dict:
    return {"actor": "ana", "deliverable": deliverable, "confidence": "high", "sources": [_src()]}


def test_a_malformed_item_no_longer_discards_the_whole_extraction() -> None:
    raw = {
        "meeting_summary": "a meeting",
        "participants": ["ana"],
        "commitments": [_ok_commitment(), {"actor": "dan", "confidence": "high", "sources": [_src()]}],
        # A decision with real substance but no `outcome` -- genuinely unusable.
        "decisions": [{"topic": "db", "confidence": "high", "sources": [_src()]}],
        "open_questions": [],
        "blockers": [],
    }
    result, dropped = validate_extraction_leniently(raw)
    # The good commitment survives, which is the entire point: it used to be discarded along with
    # the bad decision.
    assert len(result.commitments) == 1
    assert result.commitments[0].deliverable == "ship the export"
    assert result.decisions == []
    # And the drops are REPORTED, naming the field, because a silent partial extraction looks
    # identical to a meeting with fewer commitments in it.
    assert len(dropped) == 2
    assert any("commitments[1]" in d and "deliverable" in d for d in dropped)
    assert any("decisions[0]" in d and "outcome" in d for d in dropped)


def test_a_clean_extraction_drops_nothing() -> None:
    raw = {
        "meeting_summary": "a meeting",
        "commitments": [_ok_commitment()],
        "decisions": [{"topic": "db", "outcome": "postgres", "confidence": "high", "sources": [_src()]}],
    }
    result, dropped = validate_extraction_leniently(raw)
    assert dropped == []
    assert len(result.commitments) == 1 and len(result.decisions) == 1


def test_a_reply_with_no_summary_is_a_hard_failure() -> None:
    """Leniency stops at the meeting level, deliberately.

    A result with no summary is not a partial success -- it means the reply was not an extraction at
    all. Treating it as one would ground a thread on an empty document, which is the failure the
    whole grounding step exists to avoid.
    """
    with pytest.raises(ValueError):
        validate_extraction_leniently({"commitments": [_ok_commitment()]})
    with pytest.raises(ValueError):
        validate_extraction_leniently({"meeting_summary": "   "})
    with pytest.raises(ValueError):
        validate_extraction_leniently("not an object")


def test_a_non_list_section_is_ignored_and_reported() -> None:
    result, dropped = validate_extraction_leniently(
        {
            "meeting_summary": "m",
            "commitments": "oops",
            "decisions": [{"topic": "t", "outcome": "o", "confidence": "low", "sources": [_src()]}],
        }
    )
    assert result.commitments == []
    assert any("commitments" in d and "not a list" in d for d in dropped)
    # And the well-formed section beside it is untouched.
    assert len(result.decisions) == 1


def test_a_decision_without_a_topic_survives() -> None:
    """`topic` is a label; `outcome` is the substance, and it stays required.

    The most common thing a local model omitted across five runs on one transcript was
    `decisions[0].topic` / `open_questions[0].topic`, and each omission cost an item that had a real
    outcome and a verifiable quote.
    """
    result, dropped = validate_extraction_leniently({
        "meeting_summary": "m",
        "decisions": [{"outcome": "send it after verification", "confidence": "high", "sources": [_src()]}],
        "open_questions": [{"question": "do we move to pg16?", "confidence": "medium", "sources": [_src()]}],
    })
    assert dropped == []
    assert result.decisions[0].topic == ""
    assert result.decisions[0].outcome == "send it after verification"
    assert result.open_questions[0].topic == ""


def test_a_nested_confidence_is_unwrapped_rather_than_dropped() -> None:
    """A local model returned `"confidence": {"confidence": "HIGH"}`. One value, and a known one."""
    result, dropped = validate_extraction_leniently({
        "meeting_summary": "m",
        "commitments": [{"actor": "ana", "deliverable": "ship it",
                         "confidence": {"confidence": "HIGH"}, "sources": [_src()]}],
    })
    assert dropped == []
    assert result.commitments[0].confidence is Confidence.HIGH


def test_an_ambiguous_confidence_object_is_still_refused() -> None:
    # Only a SINGLE-key dict is unwrapped. Two candidate values means guessing, and this never guesses.
    _result, dropped = validate_extraction_leniently({
        "meeting_summary": "m",
        "commitments": [{"actor": "ana", "deliverable": "ship it",
                         "confidence": {"a": "high", "b": "low"}, "sources": [_src()]}],
    })
    assert any("commitments[0]" in d and "confidence" in d for d in dropped)
