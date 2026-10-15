"""Pydantic models for extracted entities.

Every extraction carries a verbatim quote from the transcript that supports it —
this is the core product promise and the reason the project is called Verbatim.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def _missing_(cls, value: object) -> Confidence | None:
        """Accept any casing, because a local model will not reliably match the schema's.

        The schema sent to a provider enumerates "high"/"medium"/"low" and a strong model with real
        tool-use follows it. Ollama's OpenAI-compatible tool calling does NOT grammar-constrain the
        output to the schema -- it free-forms the JSON -- so a local model returns "HIGH" or "High"
        and pydantic rejected the WHOLE extraction: one casing difference discarded every commitment,
        decision, question and blocker in the meeting.

        That was measured, not theorised: qwen2.5:7b on a real standup transcript produced correct
        content in the right shape and failed with ten validation errors, five of them this.

        Case is not meaning. The vocabulary stays closed -- an unrecognised word still raises -- so
        this widens the accepted SPELLING of three values and nothing else. It matters because
        on-prem Gather runs against whatever local model the customer has, which is the whole premise
        of the product, and "works only with Claude" is not an option there.
        """
        # A single-key dict is unwrapped as well: a local model returned
        # `"confidence": {"confidence": "HIGH"}`, nesting the scalar inside an object named after
        # itself. That is unambiguous -- there is exactly one value and it is a known one -- and
        # rejecting it would discard a real commitment over a wrapper. Only a single-key dict, so
        # nothing with two candidate values is ever guessed at.
        if isinstance(value, dict) and len(value) == 1:
            value = next(iter(value.values()))
        if isinstance(value, str):
            match = value.strip().lower()
            for member in cls:
                if member.value == match:
                    return member
        return None


class SourceReference(BaseModel):
    """A pointer back to the transcript text that supports an extraction."""

    verbatim_quote: str = Field(
        ...,
        description="The exact text from the transcript that supports this extraction. Must be a verbatim substring of the input, never paraphrased.",
    )
    speaker: str | None = Field(
        None,
        description="The speaker who said the quote, if identifiable from the transcript.",
    )
    approximate_timestamp: str | None = Field(
        None,
        description="Approximate timestamp from the transcript, e.g. '00:14:23', if available.",
    )
    rationale: str = Field(
        "",
        description="One sentence explaining how this quote supports the extraction.",
    )
    # OPTIONAL, with an empty default, and that is a correction rather than a loosening.
    #
    # Every consumer in this codebase already treats it as optional -- renderers.py:135,
    # simplify.py:93, cli.py:2949/2965, slack_bot.py:167/230, linear.py:241,
    # github_issues.py:141 all guard with `if ... rationale:` -- and the sibling field on
    # Decision is already `str | None`. Requiring it here was the outlier.
    #
    # What made it worth fixing: the QUOTE is the load-bearing artefact. Quote verification
    # checks that it is a real substring of the transcript, which is what stops a fabricated
    # action item reaching a team. The rationale is explanatory prose on top of it. Requiring
    # the prose meant a model that supplied a perfectly verifiable quote had its entire
    # extraction rejected for omitting a sentence nobody depends on -- losing real data to
    # enforce a nice-to-have.
    #
    # "" rather than None because store.py's `rationale TEXT NOT NULL` column takes it
    # directly, and every reader guards on truthiness, so "" renders as absent.


class Commitment(BaseModel):
    """Someone agreeing to deliver something, ideally by a specific time."""

    actor: str = Field(..., description="The person making the commitment.")
    deliverable: str = Field(..., description="What they will deliver or do.")
    deadline: str | None = Field(
        None,
        description="When it's due. Use the speaker's words if a specific date isn't given (e.g. 'by EOD Friday', 'next week').",
    )
    to: str | None = Field(
        None,
        description="Who the commitment is made to, if directed at someone specific.",
    )
    confidence: Confidence = Field(
        ...,
        description="HIGH = explicit, unambiguous commitment. MEDIUM = clearly implied. LOW = ambiguous or possibly hypothetical.",
    )
    sources: list[SourceReference] = Field(
        ..., min_length=1, description="At least one verbatim quote supporting this commitment."
    )
    notes: str | None = Field(
        None, description="Any context the reader needs that isn't in the quote itself."
    )


class Decision(BaseModel):
    """A choice the group made between options."""

    # OPTIONAL, for the same reason `rationale` is, and with the same line drawn: the SUBSTANCE of
    # this item is carried by the field below it (`outcome` for a decision, `question` for an open
    # question) and both of those stay required. `topic` is a label on top of the substance.
    #
    # Measured: `decisions[0].topic` and `open_questions[0].topic` were the single most common thing
    # a local model omitted across five runs on one real transcript, and each omission cost that
    # whole item -- a decision with a real outcome and a verifiable quote, discarded for want of a
    # label. "" rather than None so every reader's truthiness guard behaves unchanged.
    topic: str = Field("", description="The thing being decided about.")
    outcome: str = Field(..., description="What was decided.")
    participants: list[str] = Field(
        default_factory=list,
        description="Who was part of the decision.",
    )
    rationale: str | None = Field(
        None, description="The reasoning given for the choice, if stated."
    )
    alternatives_considered: list[str] = Field(
        default_factory=list,
        description="Other options that were discussed and rejected.",
    )
    confidence: Confidence = Field(
        ...,
        description="HIGH = explicitly decided. MEDIUM = clearly implied. LOW = tentative or partial.",
    )
    sources: list[SourceReference] = Field(..., min_length=1)


class OpenQuestion(BaseModel):
    """A question raised but not resolved in the transcript."""

    # OPTIONAL, for the same reason `rationale` is, and with the same line drawn: the SUBSTANCE of
    # this item is carried by the field below it (`outcome` for a decision, `question` for an open
    # question) and both of those stay required. `topic` is a label on top of the substance.
    #
    # Measured: `decisions[0].topic` and `open_questions[0].topic` were the single most common thing
    # a local model omitted across five runs on one real transcript, and each omission cost that
    # whole item -- a decision with a real outcome and a verifiable quote, discarded for want of a
    # label. "" rather than None so every reader's truthiness guard behaves unchanged.
    topic: str = Field("", description="What the question is about.")
    question: str = Field(..., description="The question itself, restated clearly.")
    raised_by: str | None = Field(None, description="Who raised it.")
    addressed_to: str | None = Field(
        None, description="Who is being asked, if directed at someone specific."
    )
    urgency: str | None = Field(
        None, description="If the speaker signalled urgency: 'high' | 'medium' | 'low'."
    )
    confidence: Confidence = Field(
        ...,
        description="HIGH = clearly an unresolved question. MEDIUM = inferred. LOW = ambiguous.",
    )
    sources: list[SourceReference] = Field(..., min_length=1)


class Blocker(BaseModel):
    """Something blocking work from progressing."""

    blocked_thing: str = Field(..., description="The work, project, or decision being blocked.")
    blocked_by: str = Field(..., description="What is blocking it — person, dependency, or external factor.")
    owner: str | None = Field(
        None, description="Who can unblock it, if identifiable."
    )
    confidence: Confidence = Field(...)
    sources: list[SourceReference] = Field(..., min_length=1)


class ExtractionResult(BaseModel):
    """The complete structured output for one transcript."""

    meeting_summary: str = Field(
        ...,
        description="A 2–3 sentence neutral summary of the meeting.",
    )
    participants: list[str] = Field(
        default_factory=list,
        description="All speakers identifiable from the transcript.",
    )
    commitments: list[Commitment] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)


def validate_extraction_leniently(
    raw: object,
) -> tuple[ExtractionResult, list[str]]:
    """Validate an extraction, dropping only the items that fail instead of the whole meeting.

    THE DEFECT THIS FIXES, measured rather than imagined. `ExtractionResult.model_validate(args)` is
    all-or-nothing: one malformed field anywhere discards every commitment, decision, question and
    blocker in the transcript. Against a local model that is not an edge case. Five runs of
    qwen2.5:7b on one real standup transcript: two valid, three rejected -- once for a nested
    `confidence`, once for a `decisions[0].topic` the model omitted, once for an `open_questions`
    field. In each rejected run the COMMITMENTS were fine and were thrown away with everything else.

    Ollama's OpenAI-compatible tool calling does not grammar-constrain output to the schema, so this
    is inherent to local inference, and on-prem Gather runs against whatever local model the customer
    has. "Works only with Claude" is not an option for an air-gapped deployment.

    So the batch is lenient and each ITEM is strict, which is the same discipline the thread renderer
    already applies on the other side of the boundary: quote verification drops an individual
    unverifiable item, counts it and logs it, rather than refusing to post anything. A meeting now
    yields whatever extracted cleanly.

    Returns the result and a list of human-readable reasons for whatever was dropped. The caller is
    expected to SURFACE that list -- a silent partial extraction is worse than a loud failure,
    because it looks like a meeting with fewer commitments in it.
    """
    dropped: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError(f"extraction payload must be an object, got {type(raw).__name__}")

    # The meeting-level fields are NOT lenient. A result with no summary is not a partial success;
    # it means the reply was not an extraction at all, and pretending otherwise would ground a
    # thread on an empty document.
    base = {
        "meeting_summary": raw.get("meeting_summary") or "",
        "participants": raw.get("participants") if isinstance(raw.get("participants"), list) else [],
    }
    if not str(base["meeting_summary"]).strip():
        raise ValueError("extraction has no meeting_summary -- the reply was not an extraction")

    models: dict[str, type[BaseModel]] = {
        "commitments": Commitment,
        "decisions": Decision,
        "open_questions": OpenQuestion,
        "blockers": Blocker,
    }
    kept: dict[str, list[Any]] = {}
    for key, model in models.items():
        items = raw.get(key)
        if not isinstance(items, list):
            if items is not None:
                dropped.append(f"{key}: not a list, ignored")
            kept[key] = []
            continue
        good: list[Any] = []
        for i, item in enumerate(items):
            try:
                good.append(model.model_validate(item))
            except ValidationError as e:
                # One line per dropped item, naming the field, so a partial extraction is visible in
                # a log rather than inferred from a count that looks low.
                fields = ", ".join(".".join(str(x) for x in err["loc"]) for err in e.errors()[:3])
                dropped.append(f"{key}[{i}]: {fields}")
        kept[key] = good

    return ExtractionResult(**base, **kept), dropped
