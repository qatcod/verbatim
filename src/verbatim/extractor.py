"""Anthropic API extraction call with forced tool use.

Tool use is how we get guaranteed structured output: we define a tool whose
input schema is our Pydantic ExtractionResult, force the model to call it,
then parse the tool input back into the Pydantic model.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from anthropic import Anthropic

from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .schema import ExtractionResult

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192


@dataclass
class ExtractionDiagnostics:
    """Metadata about the extraction run — useful for debugging quality."""

    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    transcript_chars: int


def extract(
    transcript: str,
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    api_key: str | None = None,
) -> tuple[ExtractionResult, ExtractionDiagnostics]:
    """Run the extraction LLM call and return the structured result + diagnostics.

    Reads `ANTHROPIC_API_KEY` from the environment unless `api_key` is passed.
    Model can be overridden via the `VERBATIM_MODEL` env var or the `model` arg.
    """
    chosen_model = model or os.environ.get("VERBATIM_MODEL") or DEFAULT_MODEL

    client = Anthropic(api_key=api_key) if api_key else Anthropic()

    tool_schema = _build_tool_schema()

    response = client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": "extract_meeting_entities"},
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(transcript=transcript),
            }
        ],
    )

    tool_use_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use_block is None:
        raise RuntimeError(
            "Model did not call the extraction tool. Stop reason: "
            f"{response.stop_reason}. Content: {response.content!r}"
        )

    result = ExtractionResult.model_validate(tool_use_block.input)

    diagnostics = ExtractionDiagnostics(
        model=chosen_model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason or "",
        transcript_chars=len(transcript),
    )
    return result, diagnostics


def _build_tool_schema() -> dict:
    """Build the Anthropic tool definition from the Pydantic schema.

    Anthropic's tool-use accepts a JSON schema directly as `input_schema`.
    Pydantic emits a schema with $defs/$refs, which Anthropic handles fine.
    """
    return {
        "name": "extract_meeting_entities",
        "description": (
            "Emit the structured extraction result for the meeting transcript. "
            "Every commitment/decision/question/blocker must include at least one "
            "verbatim quote from the transcript that supports it."
        ),
        "input_schema": ExtractionResult.model_json_schema(),
    }
