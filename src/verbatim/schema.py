"""Pydantic models for extracted entities.

Every extraction carries a verbatim quote from the transcript that supports it —
this is the core product promise and the reason the project is called Verbatim.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


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
        ...,
        description="One sentence explaining how this quote supports the extraction.",
    )


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

    topic: str = Field(..., description="The thing being decided about.")
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

    topic: str = Field(..., description="What the question is about.")
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
