"""Tests for the v0.14.0 watchdog — silent-failure detection + Slack webhook alerts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

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


# ----- check functions -----


def test_check_supervisord_alive_running() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = watchdog.check_supervisord_alive()
    assert result.status == "ok"


def test_check_supervisord_alive_dead() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        result = watchdog.check_supervisord_alive()
    assert result.status == "fail"
    assert "supervisord" in result.detail.lower()


def test_check_daemon_running() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "verbatim-slack-bot               RUNNING   pid 1247, uptime 0:12:30"
        )
        result = watchdog.check_daemon("verbatim-slack-bot")
    assert result.status == "ok"


def test_check_daemon_fatal() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 3
        mock_run.return_value.stdout = (
            "verbatim-slack-bot               FATAL     Exited too quickly"
        )
        result = watchdog.check_daemon("verbatim-slack-bot")
    assert result.status == "fail"
    assert "FATAL" in result.detail


def test_check_slack_api_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "team": "T123"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = watchdog.check_slack_api("xoxb-fake", client=client)
    assert result.status == "ok"


def test_check_slack_api_invalid_auth() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = watchdog.check_slack_api("xoxb-fake", client=client)
    assert result.status == "fail"
    assert "invalid_auth" in result.detail


def test_check_anthropic_api_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # 400 with a non-credit message is treated as ok for the "reachable" probe
        # — we're only screening for 402 credit exhaustion.
        return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = watchdog.check_anthropic_api("sk-ant-fake", client=client)
    assert result.status == "ok"


def test_check_anthropic_api_credit_exhausted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low",
                },
            },
        )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = watchdog.check_anthropic_api("sk-ant-fake", client=client)
    assert result.status == "fail"
    assert "credit" in result.detail.lower()


def test_check_db_silence_fresh_writes(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE entities (id TEXT, created_at TEXT)")
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(hours=2)).isoformat()
    conn.execute("INSERT INTO entities VALUES ('a', ?)", (fresh,))
    conn.commit()
    conn.close()
    result = watchdog.check_db_silence(db_path, now=now)
    assert result.status == "ok"


def test_check_db_silence_warn_boundary(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE entities (id TEXT, created_at TEXT)")
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    quiet = (now - timedelta(hours=30)).isoformat()  # 30h ago → past 24h warn threshold
    conn.execute("INSERT INTO entities VALUES ('a', ?)", (quiet,))
    conn.commit()
    conn.close()
    result = watchdog.check_db_silence(db_path, now=now)
    assert result.status == "warn"


def test_check_db_silence_fail_boundary(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE entities (id TEXT, created_at TEXT)")
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    silent = (now - timedelta(hours=60)).isoformat()  # 60h ago → past 48h fail threshold
    conn.execute("INSERT INTO entities VALUES ('a', ?)", (silent,))
    conn.commit()
    conn.close()
    result = watchdog.check_db_silence(db_path, now=now)
    assert result.status == "fail"


def test_check_canary_freshness_fresh() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    canary = (now - timedelta(minutes=3)).isoformat()
    result = watchdog.check_canary_freshness(canary, now=now)
    assert result.status == "ok"


def test_check_canary_freshness_stale() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    canary = (now - timedelta(minutes=20)).isoformat()  # >15 min stale
    result = watchdog.check_canary_freshness(canary, now=now)
    assert result.status == "fail"
    assert "stale" in result.detail.lower()


def test_check_canary_freshness_missing() -> None:
    # First-ever run: no canary yet. Treated as ok (not stale — never existed).
    result = watchdog.check_canary_freshness(None)
    assert result.status == "ok"


def test_safe_check_wraps_exceptions() -> None:
    def raises():
        raise RuntimeError("kaboom")
    result = watchdog.safe_check(raises, name="explosive")
    assert result.name == "explosive"
    assert result.status == "fail"
    assert "RuntimeError" in result.detail
    assert "kaboom" in result.detail


def test_safe_check_passes_through_normal_result() -> None:
    def normal():
        return watchdog.Check(name="calm", status="ok", detail="everything fine")
    result = watchdog.safe_check(normal, name="calm")
    assert result.status == "ok"
    assert result.detail == "everything fine"


# ----- alert rendering + webhook -----


def test_human_duration_minutes() -> None:
    assert watchdog.human_duration(180) == "3m"


def test_human_duration_hours_minutes() -> None:
    assert watchdog.human_duration(3600 + 12 * 60) == "1h 12m"


def test_human_duration_days_hours() -> None:
    assert watchdog.human_duration(86400 + 4 * 3600) == "1d 4h"


def test_render_alert_down_contains_name_and_duration() -> None:
    now = datetime(2026, 7, 16, 12, 4, 0, tzinfo=timezone.utc)
    prev = watchdog.CheckState(
        status="ok",
        since=(now - timedelta(minutes=4)).isoformat(),
        last_alerted=None, detail="",
    )
    check = watchdog.Check(name="verbatim-slack-bot", status="fail", detail="FATAL")
    payload = watchdog.render_alert(check, prev, now=now, kind="down")
    text = payload["text"]
    assert "verbatim-slack-bot" in text
    assert "DOWN" in text.upper()
    assert "FATAL" in text


def test_render_alert_recovered_contains_duration() -> None:
    now = datetime(2026, 7, 16, 12, 47, 0, tzinfo=timezone.utc)
    prev = watchdog.CheckState(
        status="fail",
        since=(now - timedelta(minutes=42)).isoformat(),
        last_alerted=(now - timedelta(minutes=30)).isoformat(),
        detail="FATAL",
    )
    check = watchdog.Check(name="verbatim-slack-bot", status="ok")
    payload = watchdog.render_alert(check, prev, now=now, kind="recovered")
    text = payload["text"]
    assert "RECOVERED" in text.upper() or "recovered" in text.lower()
    assert "42m" in text or "42 m" in text


def test_render_alert_reminder_contains_still_down() -> None:
    now = datetime(2026, 7, 16, 13, 26, 0, tzinfo=timezone.utc)
    prev = watchdog.CheckState(
        status="fail",
        since=(now - timedelta(hours=1, minutes=12)).isoformat(),
        last_alerted=(now - timedelta(minutes=32)).isoformat(),
        detail="FATAL",
    )
    check = watchdog.Check(name="verbatim-slack-bot", status="fail", detail="FATAL")
    payload = watchdog.render_alert(check, prev, now=now, kind="reminder")
    text = payload["text"]
    assert "still" in text.lower()
    assert "1h 12m" in text


def test_render_systemic_alert_lists_all_names_no_details() -> None:
    checks = [
        watchdog.Check(name="a", status="fail", detail="d1"),
        watchdog.Check(name="b", status="fail", detail="d2"),
        watchdog.Check(name="c", status="fail", detail="d3"),
        watchdog.Check(name="d", status="fail", detail="d4"),
        watchdog.Check(name="e", status="fail", detail="d5"),
        watchdog.Check(name="f", status="fail", detail="d6"),
    ]
    payload = watchdog.render_systemic_alert(checks)
    text = payload["text"]
    for name in "abcdef":
        assert name in text
    # Should NOT dump each detail — the point is to coalesce, not spam.
    assert "d1" not in text and "d6" not in text


def test_post_webhook_ok_first_try() -> None:
    calls: list[dict] = []
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append({"body": req.read()})
        return httpx.Response(200, text="ok")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok = watchdog.post_webhook(
        "https://hooks.slack.com/services/T/B/X",
        payload={"text": "hi"},
        client=client,
    )
    assert ok is True
    assert len(calls) == 1


def test_post_webhook_retries_once_on_failure() -> None:
    call_count = {"n": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok = watchdog.post_webhook(
        "https://hooks.slack.com/services/T/B/X",
        payload={"text": "hi"},
        client=client,
    )
    assert ok is True
    assert call_count["n"] == 2


def test_post_webhook_gives_up_after_retry(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    log_path = tmp_path / "watchdog.log"
    with patch.object(watchdog, "LOG_PATH", log_path):
        ok = watchdog.post_webhook(
            "https://hooks.slack.com/services/T/B/X",
            payload={"text": "hi"},
            client=client,
        )
    assert ok is False
