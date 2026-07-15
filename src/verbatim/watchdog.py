"""Verbatim watchdog — cron-driven silent-failure detection with Slack webhook alerts.

Runs every 2 minutes via cron. Probes seven signals (supervisord alive, each daemon
RUNNING, Slack + Anthropic API reachable, DB write freshness), diffs the current
result against `/opt/verbatim/health/state.json`, and POSTs an alert to a Slack
Incoming Webhook only on state transitions or 30-minute stuck-in-fail reminders.

Independent of every daemon it monitors: alerts fire even when the entire
Verbatim stack is down.

See docs/superpowers/specs/2026-07-16-watchdog-observability-design.md for the
full design.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx

# ----- paths + config -----

STATE_PATH = Path("/opt/verbatim/health/state.json")
LOG_PATH = Path("/opt/verbatim/logs/watchdog.log")
LOCK_PATH = Path("/opt/verbatim/health/watchdog.lock")

CheckStatus = Literal["ok", "warn", "fail"]


# ----- data types -----


@dataclass
class Check:
    """One probe result. Produced by check_*() functions."""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass
class CheckState:
    """Persisted state for one check between watchdog invocations."""

    status: CheckStatus
    since: str  # ISO-8601 UTC, when the check entered its current status
    last_alerted: str | None = None  # ISO-8601 UTC or None
    detail: str = ""


# ----- state I/O -----


def load_state(path: Path = STATE_PATH) -> dict[str, CheckState]:
    """Read persisted state; return {} on missing / corrupted file.

    Corruption is logged to stderr so a human can grep later. Missing is silent
    — that's the expected first-run state.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(
            f"watchdog: state file corrupt ({path}): {exc}. Treating as empty.",
            file=sys.stderr,
        )
        return {}
    checks_raw = raw.get("checks") or {}
    out: dict[str, CheckState] = {}
    for name, blob in checks_raw.items():
        out[name] = CheckState(
            status=blob.get("status", "ok"),
            since=blob.get("since", _now_iso()),
            last_alerted=blob.get("last_alerted"),
            detail=blob.get("detail", ""),
        )
    return out


def save_state(
    path: Path, checks: dict[str, CheckState], canary: str,
) -> None:
    """Write state atomically via `.tmp` + rename.

    A crashed writer never leaves a torn file for the next tick to read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checks": {name: asdict(state) for name, state in checks.items()},
        "canary": canary,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.rename(tmp, path)


# ----- helpers -----


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----- checks -----


def check_supervisord_alive() -> Check:
    """Is supervisord running at all? Uses pgrep, not supervisorctl (which needs
    the supervisord socket)."""
    proc = subprocess.run(
        ["pgrep", "-x", "supervisord"], capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return Check(name="supervisord", status="ok")
    return Check(
        name="supervisord",
        status="fail",
        detail="supervisord process not found via pgrep",
    )


def check_daemon(name: str) -> Check:
    """Is the named supervisord-managed daemon in RUNNING state?"""
    proc = subprocess.run(
        ["supervisorctl", "status", name],
        capture_output=True, text=True,
    )
    output = (proc.stdout or "").strip()
    if "RUNNING" in output:
        return Check(name=name, status="ok", detail=output)
    return Check(name=name, status="fail", detail=output or "no output")


def check_slack_api(
    bot_token: str, *, client: httpx.Client | None = None,
) -> Check:
    """Slack auth.test — verifies the bot token is still valid."""
    owned = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        resp = c.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        data = resp.json()
    finally:
        if owned:
            c.close()
    if data.get("ok") is True:
        return Check(name="slack-api", status="ok")
    err = data.get("error", "unknown")
    return Check(name="slack-api", status="fail", detail=f"slack auth.test error: {err}")


def check_anthropic_api(
    api_key: str, *, client: httpx.Client | None = None,
) -> Check:
    """Reachability probe for the Anthropic API. Specifically flags credit
    exhaustion (400 with 'credit balance' message) as fail. All other 4xx
    responses are treated as ok — the endpoint is reachable, we just deliberately
    sent an invalid request."""
    owned = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        resp = c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": "claude-sonnet-4-6", "max_tokens": 1, "messages": []},
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
    finally:
        if owned:
            c.close()
    message = (data.get("error") or {}).get("message", "")
    if "credit" in message.lower():
        return Check(
            name="anthropic-api", status="fail",
            detail=f"credit exhausted: {message}",
        )
    return Check(name="anthropic-api", status="ok")


def check_db_silence(
    db_path: Path,
    *,
    warn_hours: int = 24,
    fail_hours: int = 48,
    now: datetime | None = None,
) -> Check:
    """Time since the newest entity insert. warn at ≥warn_hours, fail at
    ≥fail_hours. Never touched the DB → status=warn with "no writes yet"."""
    now = now or datetime.now(timezone.utc)
    if not db_path.exists():
        return Check(name="db-silence", status="warn", detail="db file missing")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(created_at) FROM entities").fetchone()
    finally:
        conn.close()
    latest = row[0] if row else None
    if not latest:
        return Check(name="db-silence", status="warn", detail="no writes yet")
    try:
        latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    except ValueError:
        return Check(
            name="db-silence", status="warn",
            detail=f"unparseable timestamp: {latest!r}",
        )
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    hours = (now - latest_dt).total_seconds() / 3600.0
    if hours >= fail_hours:
        return Check(
            name="db-silence", status="fail",
            detail=f"no new items for {hours:.1f}h (≥{fail_hours}h)",
        )
    if hours >= warn_hours:
        return Check(
            name="db-silence", status="warn",
            detail=f"no new items for {hours:.1f}h",
        )
    return Check(name="db-silence", status="ok")


def check_canary_freshness(
    state_canary: str | None,
    *,
    stale_after_min: int = 15,
    now: datetime | None = None,
) -> Check:
    """'Watchdog watches watchdog': if the previous tick's canary timestamp is
    older than stale_after_min, this run detects that the watchdog itself has
    not been running. First-ever run (canary is None) returns ok."""
    if state_canary is None:
        return Check(name="canary", status="ok")
    now = now or datetime.now(timezone.utc)
    try:
        canary_dt = datetime.fromisoformat(state_canary.replace("Z", "+00:00"))
    except ValueError:
        return Check(
            name="canary", status="fail",
            detail=f"unparseable canary timestamp: {state_canary!r}",
        )
    if canary_dt.tzinfo is None:
        canary_dt = canary_dt.replace(tzinfo=timezone.utc)
    minutes = (now - canary_dt).total_seconds() / 60.0
    if minutes > stale_after_min:
        return Check(
            name="canary", status="fail",
            detail=f"canary stale — last watchdog run {minutes:.1f}m ago",
        )
    return Check(name="canary", status="ok")


def safe_check(fn: Callable[[], Check], *, name: str) -> Check:
    """Wrap a check_* invocation. If it raises, produce a fail Check with the
    exception details, so a broken check appears as an alert rather than
    crashing the whole watchdog."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return Check(
            name=name, status="fail",
            detail=f"check function raised: {type(exc).__name__}: {exc}",
        )


# ----- alert rendering -----


def human_duration(seconds: float) -> str:
    """Compact human duration: '3m', '1h 12m', '1d 4h'. Rounds down."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        if rem_min:
            return f"{hours}h {rem_min}m"
        return f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    if rem_h:
        return f"{days}d {rem_h}h"
    return f"{days}d"


def render_alert(
    check: Check,
    previous: CheckState | None,
    *,
    now: datetime,
    kind: Literal["down", "recovered", "reminder"],
) -> dict[str, str]:
    """Slack text payload for one check's transition or reminder."""
    if kind == "down":
        # duration = how long since we were last ok. previous.since is when the
        # ok run began, so "down since NOW".
        text = (
            f":rotating_light: *{check.name}* is DOWN\n"
            f"down since: {now.isoformat()} (just now)\n"
            f"detail: {check.detail or '(no detail)'}\n"
            f"fix: ssh into the pod, run "
            f"`supervisorctl restart {check.name}` or check "
            f"`/opt/verbatim/logs/{check.name}.err.log`"
        )
        return {"text": text}
    if kind == "recovered":
        down_since = (
            datetime.fromisoformat(previous.since.replace("Z", "+00:00"))
            if previous else now
        )
        if down_since.tzinfo is None:
            down_since = down_since.replace(tzinfo=timezone.utc)
        duration = human_duration((now - down_since).total_seconds())
        text = (
            f":white_check_mark: *{check.name}* RECOVERED\n"
            f"was down for: {duration}\n"
            f"now: {check.detail or 'ok'}"
        )
        return {"text": text}
    # kind == "reminder"
    if previous is None:
        return {"text": f":rotating_light: *{check.name}* STILL DOWN"}
    down_since = datetime.fromisoformat(previous.since.replace("Z", "+00:00"))
    if down_since.tzinfo is None:
        down_since = down_since.replace(tzinfo=timezone.utc)
    duration = human_duration((now - down_since).total_seconds())
    text = (
        f":rotating_light: *{check.name}* STILL DOWN\n"
        f"down for: {duration}\n"
        f"detail: {check.detail or previous.detail or '(no detail)'}"
    )
    return {"text": text}


def render_systemic_alert(failed_checks: list[Check]) -> dict[str, str]:
    """Coalesced alert for a total-outage situation: lists names, no per-check
    details."""
    names = ", ".join(c.name for c in failed_checks)
    text = (
        f":rotating_light: *SYSTEMIC OUTAGE* — "
        f"{len(failed_checks)} checks failed in this tick\n"
        f"{names}\n"
        f"fix: investigate pod networking / power / DNS. "
        f"Individual check details in `/opt/verbatim/logs/watchdog.log`."
    )
    return {"text": text}


# ----- webhook client -----


def post_webhook(
    webhook_url: str,
    payload: dict[str, str],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """POST payload to a Slack Incoming Webhook. One retry with 2s backoff.
    Returns True on success (2xx), False after retry exhaustion. Logs failure
    to LOG_PATH so the payload isn't lost."""
    owned = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        for attempt in range(2):
            try:
                resp = c.post(webhook_url, json=payload)
                if 200 <= resp.status_code < 300:
                    return True
            except httpx.RequestError:
                pass
            if attempt == 0:
                time.sleep(2)
        # Both attempts failed.
        _log_dropped_alert(payload)
        return False
    finally:
        if owned:
            c.close()


def _log_dropped_alert(payload: dict[str, str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(
            f"[{_now_iso()}] webhook POST failed after retry. "
            f"Dropped payload: {json.dumps(payload)}\n"
        )
