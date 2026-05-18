"""Transcript parser tests."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from verbatim.transcripts import load_transcript


def test_plain_text_loads(tmp_path: Path) -> None:
    p = tmp_path / "meeting.txt"
    p.write_text("[00:00] Qat: hello\n[00:05] Jason: hi\n", encoding="utf-8")
    out = load_transcript(p)
    assert "Qat: hello" in out
    assert "Jason: hi" in out


def test_collapses_blank_runs(tmp_path: Path) -> None:
    p = tmp_path / "meeting.txt"
    p.write_text("line one\n\n\n\nline two\n", encoding="utf-8")
    out = load_transcript(p)
    # multiple blank lines collapse to at most one
    assert "\n\n\n" not in out
    assert "line one" in out
    assert "line two" in out


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_transcript(tmp_path / "nope.txt")


def test_vtt_parsing(tmp_path: Path) -> None:
    vtt = (
        "WEBVTT\n"
        "\n"
        "1\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "Qat: opening line\n"
        "\n"
        "2\n"
        "00:00:03.500 --> 00:00:06.000\n"
        "Jason: response line\n"
    )
    p = tmp_path / "meeting.vtt"
    p.write_text(vtt, encoding="utf-8")
    out = load_transcript(p)
    assert "[00:00:00] Qat: opening line" in out
    assert "[00:00:03] Jason: response line" in out


def test_vtt_skips_notes(tmp_path: Path) -> None:
    vtt = (
        "WEBVTT\n"
        "\n"
        "NOTE This is a note\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "Qat: hello\n"
    )
    p = tmp_path / "meeting.vtt"
    p.write_text(vtt, encoding="utf-8")
    out = load_transcript(p)
    assert "NOTE" not in out
    assert "Qat: hello" in out


def test_stdin_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped content\nsecond line\n"))
    out = load_transcript("-")
    assert "piped content" in out
    assert "second line" in out


def test_empty_file_returns_just_newline(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    out = load_transcript(p)
    assert out.strip() == ""
