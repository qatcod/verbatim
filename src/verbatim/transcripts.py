"""Transcript format parsers.

v0 supports plain text and WebVTT (.vtt). Both are normalized to a single string
that the extractor reads — the LLM is responsible for interpreting structure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def load_transcript(source: str | Path) -> str:
    """Load a transcript from a file path or '-' for stdin.

    Auto-detects format by extension; falls back to plain text.
    """
    if source == "-" or source == Path("-"):
        return _normalize_plain(sys.stdin.read())

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Transcript file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix == ".vtt":
        return _parse_vtt(raw)
    return _normalize_plain(raw)


def _normalize_plain(text: str) -> str:
    """Strip surrounding whitespace, collapse internal blank-line runs."""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                out.append("")
        else:
            blank_run = 0
            out.append(line)
    return "\n".join(out).strip() + "\n"


_VTT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*$"
)
_VTT_SHORT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}\.\d{3}.*$")


def _parse_vtt(raw: str) -> str:
    """Parse WebVTT into a flat transcript with timestamps and speaker labels preserved.

    WebVTT cue format:
        00:01:23.000 --> 00:01:28.000
        Speaker Name: line of dialogue

    We collapse this into:
        [00:01:23] Speaker Name: line of dialogue
    """
    lines = raw.splitlines()
    out: list[str] = []
    current_timestamp: str | None = None
    current_cue: list[str] = []

    def flush():
        nonlocal current_cue, current_timestamp
        if current_cue:
            text = " ".join(current_cue).strip()
            if text:
                prefix = f"[{current_timestamp}] " if current_timestamp else ""
                out.append(f"{prefix}{text}")
        current_cue = []
        current_timestamp = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped == "WEBVTT" or stripped.startswith("NOTE "):
            continue
        if _VTT_TIMESTAMP_RE.match(stripped) or _VTT_SHORT_TIMESTAMP_RE.match(stripped):
            flush()
            current_timestamp = stripped.split("-->")[0].strip().rsplit(".", 1)[0]
            continue
        # cue identifier lines (numeric or arbitrary, alone on a line) — skip
        if stripped.isdigit() and not current_cue:
            continue
        current_cue.append(stripped)

    flush()
    return "\n".join(out).strip() + "\n"
