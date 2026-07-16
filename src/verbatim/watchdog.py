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

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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


@dataclass
class Alert:
    """A pending alert ready to dispatch after state has been persisted."""

    kind: Literal["down", "recovered", "reminder", "systemic"]
    checks: list[Check]  # single-element for down/recovered/reminder; multi for systemic
    previous_states: list[CheckState | None]  # aligned with checks; empty for systemic


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
        ["pgrep", "-x", "supervisord"], capture_output=True, text=True, timeout=5,
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
        capture_output=True, text=True, timeout=5,
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


# ----- state machine -----


def diff_state(
    current: list[Check],
    previous: dict[str, CheckState],
    *,
    reminder_min: int,
    systemic_threshold: int = 5,
    now: datetime,
) -> tuple[dict[str, CheckState], list[Alert]]:
    """Pure computation: diff current check results against previous state and
    return the new state dict plus a list of Alert objects to dispatch.
    No side effects — no POSTing, no saving.
    """
    updated: dict[str, CheckState] = {}
    down_transitions: list[Check] = []
    down_previous: list[CheckState | None] = []
    reminders: list[tuple[Check, CheckState]] = []
    recoveries: list[tuple[Check, CheckState]] = []

    for check in current:
        old = previous.get(check.name)
        if old is None:
            # First-run: seed. If already failing, treat as a down_transition
            # to permit systemic coalescing on boot.
            if check.status == "fail":
                down_transitions.append(check)
                down_previous.append(None)
            updated[check.name] = CheckState(
                status=check.status,
                since=now.isoformat(),
                last_alerted=now.isoformat() if check.status == "fail" else None,
                detail=check.detail,
            )
            continue

        transitioned = old.status != check.status
        if transitioned:
            if check.status == "fail":
                down_transitions.append(check)
                down_previous.append(old)
            elif old.status == "fail" and check.status == "ok":
                recoveries.append((check, old))
            # warn transitions (ok→warn, warn→ok, warn→warn) don't alert here.
            updated[check.name] = CheckState(
                status=check.status,
                since=now.isoformat(),
                last_alerted=now.isoformat() if check.status == "fail" else None,
                detail=check.detail,
            )
        else:
            # Same status. Check for stuck-in-fail reminder.
            stuck_reminder = False
            if (
                check.status == "fail"
                and old.last_alerted is not None
            ):
                last_alerted_dt = datetime.fromisoformat(
                    old.last_alerted.replace("Z", "+00:00"),
                )
                if last_alerted_dt.tzinfo is None:
                    last_alerted_dt = last_alerted_dt.replace(tzinfo=timezone.utc)
                minutes = (now - last_alerted_dt).total_seconds() / 60.0
                if minutes >= reminder_min:
                    stuck_reminder = True

            if stuck_reminder:
                reminders.append((check, old))
                updated[check.name] = CheckState(
                    status=check.status,
                    since=old.since,
                    last_alerted=now.isoformat(),
                    detail=check.detail,
                )
            else:
                updated[check.name] = CheckState(
                    status=check.status,
                    since=old.since,
                    last_alerted=old.last_alerted,
                    detail=check.detail,
                )

    # Build alert list. Systemic coalescing applies only to same-tick down transitions.
    alerts: list[Alert] = []
    if len(down_transitions) >= systemic_threshold:
        alerts.append(Alert(kind="systemic", checks=list(down_transitions), previous_states=[]))
    else:
        for check, prev_state in zip(down_transitions, down_previous, strict=True):
            alerts.append(Alert(kind="down", checks=[check], previous_states=[prev_state]))

    for check, old in recoveries:
        alerts.append(Alert(kind="recovered", checks=[check], previous_states=[old]))

    for check, old in reminders:
        alerts.append(Alert(kind="reminder", checks=[check], previous_states=[old]))

    return updated, alerts


def _dispatch_alerts(
    alerts: list[Alert],
    webhook_url: str,
    *,
    now: datetime,
    post_fn: Callable[..., bool],
) -> None:
    """Send a list of Alert objects via post_fn. Called only after state is saved."""
    for alert in alerts:
        if alert.kind == "systemic":
            post_fn(webhook_url, render_systemic_alert(alert.checks))
        else:
            check = alert.checks[0]
            prev_state = alert.previous_states[0] if alert.previous_states else None
            post_fn(
                webhook_url,
                render_alert(check, prev_state, now=now, kind=alert.kind),  # type: ignore[arg-type]
            )


def diff_and_alert(
    current: list[Check],
    previous: dict[str, CheckState],
    webhook_url: str,
    reminder_min: int,
    *,
    now: datetime,
    systemic_threshold: int = 5,
    post_fn: Callable[..., bool] | None = None,
) -> dict[str, CheckState]:
    """Backward-compat wrapper: compute new state + alerts, dispatch immediately.

    Existing callers (and all tests) continue to work unchanged. For the
    production path, use diff_state + save_state + _dispatch_alerts instead so
    that alerts are only sent after state has been successfully persisted.
    """
    post = post_fn or post_webhook
    updated, alerts = diff_state(
        current, previous,
        reminder_min=reminder_min,
        systemic_threshold=systemic_threshold,
        now=now,
    )
    _dispatch_alerts(alerts, webhook_url, now=now, post_fn=post)
    return updated


# ----- config -----


@dataclass
class WatchdogConfig:
    webhook_url: str
    reminder_min: int
    silence_hours: int
    silence_fail_hours: int
    bot_token: str | None
    anthropic_api_key: str | None
    db_path: Path
    daemons: list[str] = field(
        default_factory=lambda: [
            "verbatim-watch", "verbatim-slack-bot", "verbatim-daily",
        ],
    )


def load_config_from_env() -> WatchdogConfig:
    """Read all env vars, apply defaults, and validate. Exit(1) on missing
    required config."""
    webhook_url = os.environ.get("VERBATIM_WATCHDOG_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(
            "ERROR: VERBATIM_WATCHDOG_WEBHOOK_URL not set in .env — "
            "watchdog can't send alerts.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return WatchdogConfig(
        webhook_url=webhook_url,
        reminder_min=int(os.environ.get("VERBATIM_WATCHDOG_REMINDER_MIN", "30")),
        silence_hours=int(os.environ.get("VERBATIM_WATCHDOG_SILENCE_HOURS", "24")),
        silence_fail_hours=int(
            os.environ.get("VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS", "48"),
        ),
        bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        db_path=Path(
            os.environ.get("VERBATIM_DB_PATH", "/opt/verbatim/state.db"),
        ),
    )


# ----- orchestrator -----


def _prior_canary(state_path: Path) -> str | None:
    if not state_path.exists():
        return None
    try:
        raw = json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return None
    return raw.get("canary")


def run_once(
    *,
    config: WatchdogConfig,
    state_path: Path = STATE_PATH,
    now: datetime | None = None,
    post_fn: Callable[..., bool] | None = None,
) -> dict[str, CheckState]:
    """Run all checks once, diff against persisted state, dispatch alerts,
    save new state, return it (for tests)."""
    now = now or datetime.now(timezone.utc)
    previous = load_state(state_path)
    prior_canary = _prior_canary(state_path)

    checks: list[Check] = [
        safe_check(check_supervisord_alive, name="supervisord"),
    ]
    for daemon in config.daemons:
        checks.append(safe_check(lambda d=daemon: check_daemon(d), name=daemon))
    if config.bot_token:
        checks.append(safe_check(
            lambda: check_slack_api(config.bot_token),  # type: ignore[arg-type]
            name="slack-api",
        ))
    if config.anthropic_api_key:
        checks.append(safe_check(
            lambda: check_anthropic_api(config.anthropic_api_key),  # type: ignore[arg-type]
            name="anthropic-api",
        ))
    checks.append(safe_check(
        lambda: check_db_silence(
            config.db_path,
            warn_hours=config.silence_hours,
            fail_hours=config.silence_fail_hours,
            now=now,
        ),
        name="db-silence",
    ))
    checks.append(safe_check(
        lambda: check_canary_freshness(prior_canary, now=now),
        name="canary",
    ))

    post = post_fn or post_webhook
    updated, alerts = diff_state(
        checks, previous,
        reminder_min=config.reminder_min,
        now=now,
    )
    try:
        save_state(state_path, updated, canary=now.isoformat())
    except OSError as e:
        print(
            f"[watchdog] save_state failed, suppressing alerts: {e}",
            file=sys.stderr,
        )
        return previous  # unchanged; next tick will retry with same previous state
    # State persisted — safe to dispatch alerts now.
    _dispatch_alerts(alerts, config.webhook_url, now=now, post_fn=post)
    return updated


# ----- main entry (flock + orchestration) -----


def _dry_run_poster(webhook_url: str, payload: dict[str, str], **kwargs) -> bool:
    print(f"DRY RUN — would POST to {webhook_url}:")
    print(json.dumps(payload, indent=2))
    return True


def main(*, dry_run: bool = False, send_canary: bool = False) -> int:
    """CLI entry point. Acquires flock, loads config, runs once. Returns exit code.
    dry_run: run all checks + print what would be sent, don't POST or save state.
    send_canary: post a 'hello from watchdog' message and exit — used at deploy time.
    """
    config = load_config_from_env()

    if send_canary:
        ok = post_webhook(
            config.webhook_url,
            {"text": f":test_tube: verbatim watchdog canary — {_now_iso()}"},
        )
        print("canary sent" if ok else "canary FAILED", file=sys.stderr)
        return 0 if ok else 1

    # Acquire an advisory lock so overlapping cron ticks don't collide.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(
            f"[{_now_iso()}] watchdog: already running (lock held). exiting.",
            file=sys.stderr,
        )
        return 0

    try:
        post_fn = _dry_run_poster if dry_run else None
        run_once(config=config, post_fn=post_fn)
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()

    return 0
