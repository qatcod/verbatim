"""Tests for the v0.14.0 watchdog — silent-failure detection + Slack webhook alerts."""
from __future__ import annotations

import json
from pathlib import Path

from verbatim import watchdog

# ----- state I/O -----


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    result = watchdog.load_state(state_path)
    assert result == {}


def test_load_state_corrupted_file_returns_empty(tmp_path: Path, capsys) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")
    result = watchdog.load_state(state_path)
    assert result == {}
    # Corruption is logged to stderr so a human can grep later.
    assert "corrupt" in capsys.readouterr().err.lower()


def test_load_state_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = {
        "supervisord": watchdog.CheckState(
            status="ok",
            since="2026-07-16T12:00:00+00:00",
            last_alerted=None,
            detail="",
        ),
    }
    watchdog.save_state(state_path, state, canary="2026-07-16T12:00:00+00:00")
    loaded = watchdog.load_state(state_path)
    assert loaded["supervisord"].status == "ok"
    assert loaded["supervisord"].since == "2026-07-16T12:00:00+00:00"
    assert loaded["supervisord"].last_alerted is None


def test_save_state_is_atomic(tmp_path: Path) -> None:
    """save_state writes via .tmp then rename — no partial writes visible."""
    state_path = tmp_path / "state.json"
    state = {
        "x": watchdog.CheckState(
            status="fail", since="2026-07-16T12:00:00+00:00",
            last_alerted="2026-07-16T12:05:00+00:00", detail="broken",
        ),
    }
    watchdog.save_state(state_path, state, canary="2026-07-16T12:05:00+00:00")
    # After successful save the .tmp file must not exist.
    assert not (tmp_path / "state.json.tmp").exists()
    # And the real file is valid JSON.
    parsed = json.loads(state_path.read_text())
    assert parsed["checks"]["x"]["status"] == "fail"
    assert parsed["canary"] == "2026-07-16T12:05:00+00:00"
