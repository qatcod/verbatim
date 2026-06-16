"""Shared pytest fixtures."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from verbatim import state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "verbatim_test.db"


@pytest.fixture
def conn(tmp_db_path: Path) -> Iterator[sqlite3.Connection]:
    c = state.open_db(tmp_db_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def sample_diagnostics() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="claude-sonnet-4-6",
        input_tokens=1234,
        output_tokens=567,
        stop_reason="tool_use",
        transcript_chars=3600,
    )


@pytest.fixture
def sample_extraction() -> ExtractionResult:
    return ExtractionResult(
        meeting_summary="Kickoff. Two commitments, one decision, one open question, one blocker.",
        participants=["Alice", "Bob", "Carol"],
        commitments=[
            Commitment(
                actor="Alice",
                deliverable="v0 of Verbatim CLI",
                deadline="EOD Wednesday",
                confidence=Confidence.HIGH,
                sources=[
                    SourceReference(
                        verbatim_quote="I'll have a working version by end of day Wednesday.",
                        speaker="Alice",
                        approximate_timestamp="00:51",
                        rationale="Explicit commitment with a clear deadline.",
                    )
                ],
            ),
            Commitment(
                actor="Carol",
                deliverable="review of v0 extraction quality",
                deadline="Thursday morning",
                to="Bob",
                confidence=Confidence.MEDIUM,
                sources=[
                    SourceReference(
                        verbatim_quote="Yeah I can do that. Probably Thursday morning.",
                        speaker="Carol",
                        rationale="Agreed to take it on.",
                    )
                ],
            ),
        ],
        decisions=[
            Decision(
                topic="language for v0",
                outcome="Python",
                participants=["Alice", "Carol"],
                rationale="Iteration speed.",
                alternatives_considered=["TypeScript"],
                confidence=Confidence.HIGH,
                sources=[
                    SourceReference(
                        verbatim_quote="Python for v0.",
                        speaker="Alice",
                        rationale="Stated decision.",
                    )
                ],
            ),
        ],
        open_questions=[
            OpenQuestion(
                topic="API cost model",
                question="What's the budget for ongoing API tokens?",
                raised_by="Carol",
                addressed_to="Bob",
                urgency="medium",
                confidence=Confidence.HIGH,
                sources=[
                    SourceReference(
                        verbatim_quote="do we have a budget for the Anthropic API tokens?",
                        speaker="Carol",
                        rationale="Question, partial answer.",
                    )
                ],
            ),
        ],
        blockers=[
            Blocker(
                blocked_thing="ship public v0",
                blocked_by="extraction quality review",
                owner="Carol",
                confidence=Confidence.MEDIUM,
                sources=[
                    SourceReference(
                        verbatim_quote="I don't want to publish a half-baked thing.",
                        speaker="Bob",
                        rationale="Gating on review.",
                    )
                ],
            ),
        ],
    )
