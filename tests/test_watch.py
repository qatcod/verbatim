"""Watch (daemon mode) tests — exercise the generic loop without real APIs."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from verbatim.cli import app


def test_watch_help() -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["watch", "--help"])
    assert r.exit_code == 0
    assert "slack-api" in r.stdout
    assert "github" in r.stdout


def test_watch_slack_api_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    runner = CliRunner()
    r = runner.invoke(app, ["watch", "slack-api", "--iterations", "1"])
    # exit code 2 = our validation rejected the run before starting the loop
    assert r.exit_code == 2


def test_watch_github_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runner = CliRunner()
    r = runner.invoke(
        app, ["watch", "github", "qatcod/verbatim", "--iterations", "1"]
    )
    assert r.exit_code == 2


def test_watch_github_rejects_bad_repo() -> None:
    runner = CliRunner()
    r = runner.invoke(
        app, ["watch", "github", "no-slash", "--token", "ghp_x", "--iterations", "1"]
    )
    assert r.exit_code == 2


def test_watch_loop_calls_poll_once_per_iteration() -> None:
    """The generic loop fires `poll_once(since)` exactly `iterations` times."""
    from verbatim.cli import _watch_loop

    calls: list[datetime] = []

    def poll_once(since: datetime) -> None:
        calls.append(since)

    # We mock time.sleep so the test is instant.
    with patch("time.sleep") as mock_sleep:
        _watch_loop(
            label="test loop",
            interval_seconds=30,
            overlap_seconds=10,
            iterations=3,
            poll_once=poll_once,
        )
    assert len(calls) == 3
    # sleep called only between iterations (after the first two of three)
    assert mock_sleep.call_count == 2
    # since arg is in the past relative to wall clock by ~40s
    for s in calls:
        assert (datetime.now(tz=s.tzinfo) - s).total_seconds() >= 39


def test_watch_loop_iteration_failure_does_not_kill_loop() -> None:
    """A failing iteration should be logged and the loop should continue."""
    from verbatim.cli import _watch_loop

    succeeds: list[Any] = []

    def poll_once(since: datetime) -> None:
        if not succeeds:
            succeeds.append("first call boom")
            raise RuntimeError("simulated network blip")
        succeeds.append("second call ok")

    with patch("time.sleep"):
        _watch_loop(
            label="resilience test",
            interval_seconds=10,
            overlap_seconds=5,
            iterations=2,
            poll_once=poll_once,
        )

    assert len(succeeds) == 2  # both iterations ran despite the first one raising
