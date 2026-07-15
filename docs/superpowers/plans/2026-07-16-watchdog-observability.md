# Watchdog Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v0.14.0 — a cron-driven watchdog that detects Verbatim's silent failures (daemon deaths, service errors, data-flow silence) and alerts the operator via a Slack Incoming Webhook, independent of the daemons being monitored.

**Architecture:** New standalone module `src/verbatim/watchdog.py` invoked as `verbatim watchdog` (a Typer subcommand). Cron runs it every 2 minutes. It probes seven signals, compares to state persisted in `/opt/verbatim/health/state.json`, and POSTs to a Slack webhook only on state transitions or 30-minute stuck-in-fail reminders. Alerts have three flavors — down, recovered, still-down — plus a systemic-outage coalescer.

**Tech Stack:** Python 3.10+, `httpx` (already in venv), `typer` (already in venv), `pytest`, `ruff`. No new runtime dependencies.

## Global Constraints

- **Python 3.10+ compatible** (project floor from `pyproject.toml`).
- **No new runtime dependencies** — `httpx` and `typer` are already installed; `flock` is `/usr/bin/flock` on the pod.
- **All timestamps ISO-8601 UTC** via `datetime.now(timezone.utc)`.
- **State file at `/opt/verbatim/health/state.json`** — exact path.
- **Log file at `/opt/verbatim/logs/watchdog.log`** — exact path.
- **Lock file at `/opt/verbatim/health/watchdog.lock`** — exact path.
- **Env vars** — `VERBATIM_WATCHDOG_WEBHOOK_URL`, `VERBATIM_WATCHDOG_REMINDER_MIN` (default 30), `VERBATIM_WATCHDOG_SILENCE_HOURS` (default 24), `VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS` (default 48).
- **Cron interval: 2 minutes** — `*/2 * * * *`.
- **Systemic threshold: 5 simultaneous fail transitions** trigger coalesced alert instead of individual ones.
- **Retry policy for webhook POST: 1 retry with 2s backoff.**
- **State file writes must be atomic** — write `.tmp` then `os.rename`.
- **Ruff must pass** — the project runs `ruff check src/ tests/` as gate.

## File Structure

**Create:**
- `src/verbatim/watchdog.py` — the whole watchdog module (~250 LOC)
- `tests/test_watchdog.py` — new test file (~29 tests)

**Modify:**
- `src/verbatim/cli.py` — add `verbatim watchdog` Typer command (near end of file)
- `src/verbatim/__init__.py` — version bump `0.13.2` → `0.14.0`
- `pyproject.toml` — version bump `0.13.2` → `0.14.0`
- `docs/index.html` — version stamp update
- `CHANGELOG.md` — add v0.14.0 entry at top

**Reference:** `docs/superpowers/specs/2026-07-16-watchdog-observability-design.md` — the approved spec.

---

## Task 1: Data types + state I/O

**Files:**
- Create: `src/verbatim/watchdog.py` (partial — data types + state I/O only)
- Create: `tests/test_watchdog.py` (partial — tests for this task's exports)

**Interfaces:**
- Produces:
  - Enum-like literal `CheckStatus = Literal["ok", "warn", "fail"]`
  - Dataclass `Check` with fields `name: str`, `status: CheckStatus`, `detail: str`
  - Dataclass `CheckState` with fields `status: CheckStatus`, `since: str` (ISO), `last_alerted: str | None`, `detail: str = ""`
  - Function `load_state(path: Path) -> dict[str, CheckState]` — returns dict keyed by check name; missing / corrupt file returns `{}` (logged to stderr on corruption).
  - Function `save_state(path: Path, checks: dict[str, CheckState], canary: str) -> None` — writes atomically via `.tmp` + `os.rename`.
  - Constants: `STATE_PATH = Path("/opt/verbatim/health/state.json")`, `LOG_PATH = Path("/opt/verbatim/logs/watchdog.log")`.

- [ ] **Step 1.1: Write failing tests for state I/O**

Create `tests/test_watchdog.py`:

```python
"""Tests for the v0.14.0 watchdog — silent-failure detection + Slack webhook alerts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `ModuleNotFoundError: No module named 'verbatim.watchdog'` or similar — module doesn't exist yet.

- [ ] **Step 1.3: Implement `src/verbatim/watchdog.py` with types + state I/O**

Create `src/verbatim/watchdog.py`:

```python
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
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `4 passed`.

- [ ] **Step 1.5: Run ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/
```
Expected: `All checks passed!` (or fix any complaints then re-run).

- [ ] **Step 1.6: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/watchdog.py tests/test_watchdog.py
git commit -m "watchdog: add data types + atomic state I/O

Foundation for v0.14.0: Check + CheckState dataclasses, load_state()
that survives missing / corrupt files, save_state() that writes
atomically via .tmp + rename. 4 tests, ruff clean."
```

---

## Task 2: Check functions + safe_check wrapper

**Files:**
- Modify: `src/verbatim/watchdog.py` — append check_*() functions + safe_check()
- Modify: `tests/test_watchdog.py` — append per-check tests

**Interfaces:**
- Consumes: `Check` dataclass (Task 1)
- Produces:
  - `check_supervisord_alive() -> Check` — pgrep supervisord
  - `check_daemon(name: str) -> Check` — `supervisorctl status <name>`
  - `check_slack_api(bot_token: str, client: httpx.Client | None = None) -> Check` — POST to Slack `auth.test`
  - `check_anthropic_api(api_key: str, client: httpx.Client | None = None) -> Check` — POST minimal request; specifically flags 402
  - `check_db_silence(db_path: Path, warn_hours: int = 24, fail_hours: int = 48, now: datetime | None = None) -> Check` — reads max(created_at) from entities
  - `check_canary_freshness(state_canary: str | None, stale_after_min: int = 15, now: datetime | None = None) -> Check`
  - `safe_check(fn: Callable[[], Check], name: str) -> Check` — wraps a check_* call, converts exceptions to Check(status="fail", detail=...)

- [ ] **Step 2.1: Write failing tests for check functions**

Append to `tests/test_watchdog.py`:

```python
# ----- check functions -----


import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx


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
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: 4 tests pass (from Task 1), 15+ fail with `AttributeError: module 'verbatim.watchdog' has no attribute 'check_*'`.

- [ ] **Step 2.3: Implement check functions**

Append to `src/verbatim/watchdog.py`:

```python
# ----- checks -----

import sqlite3
import subprocess
from datetime import timedelta
from typing import Callable

import httpx


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
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `19 passed`.

- [ ] **Step 2.5: Ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/ --fix
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: ruff clean, 19 tests pass.

- [ ] **Step 2.6: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/watchdog.py tests/test_watchdog.py
git commit -m "watchdog: add 7 check_* probes + safe_check wrapper

check_supervisord_alive (pgrep), check_daemon (supervisorctl status),
check_slack_api (auth.test), check_anthropic_api (specifically catches
402 credit-exhaustion), check_db_silence (warn 24h / fail 48h),
check_canary_freshness (watchdog-watches-watchdog), and safe_check that
turns exceptions into fail Checks rather than crashing.

15 new tests. Full watchdog suite: 19. Ruff clean."
```

---

## Task 3: Webhook client + alert payload rendering

**Files:**
- Modify: `src/verbatim/watchdog.py` — append Alert dataclass + render_alert + post_webhook
- Modify: `tests/test_watchdog.py` — append tests

**Interfaces:**
- Consumes: `Check` and `CheckState` (Task 1)
- Produces:
  - Function `render_alert(check: Check, previous: CheckState | None, now: datetime, kind: Literal["down", "recovered", "reminder", "systemic"]) -> dict[str, str]` — returns a payload dict with keys `text` (main line for Slack), `blocks` (Block Kit list) or falls back to text-only. For simplicity we emit `{"text": "..."}` — Slack renders that directly.
  - Function `render_systemic_alert(failed_checks: list[Check]) -> dict[str, str]`
  - Function `post_webhook(webhook_url: str, payload: dict[str, str], client: httpx.Client | None = None) -> bool` — POST + retry once with 2s backoff. Returns True on success, False after retry exhaustion (logs to LOG_PATH).
  - Function `human_duration(delta_seconds: float) -> str` — "3m", "1h 12m", "1d 4h"

- [ ] **Step 3.1: Write failing tests**

Append to `tests/test_watchdog.py`:

```python
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


def test_post_webhook_gives_up_after_retry() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok = watchdog.post_webhook(
        "https://hooks.slack.com/services/T/B/X",
        payload={"text": "hi"},
        client=client,
    )
    assert ok is False
```

- [ ] **Step 3.2: Verify tests fail**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: previous 19 pass, 10 new fail with `AttributeError`.

- [ ] **Step 3.3: Implement alert rendering + webhook**

Append to `src/verbatim/watchdog.py`:

```python
# ----- alert rendering -----

import time


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
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `29 passed`.

- [ ] **Step 3.5: Ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/ --fix
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: ruff clean, 29 tests pass.

- [ ] **Step 3.6: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/watchdog.py tests/test_watchdog.py
git commit -m "watchdog: alert rendering + webhook client with retry

human_duration for compact durations, render_alert for down/recovered/
reminder text, render_systemic_alert for coalesced outage messages,
post_webhook with 1 retry + 2s backoff + dropped-payload logging.

10 new tests. Full watchdog suite: 29. Ruff clean."
```

---

## Task 4: State machine (diff_and_alert + systemic coalescing)

**Files:**
- Modify: `src/verbatim/watchdog.py` — append diff_and_alert()
- Modify: `tests/test_watchdog.py` — append state-machine tests

**Interfaces:**
- Consumes: `Check`, `CheckState`, `render_alert`, `render_systemic_alert`, `post_webhook` (previous tasks)
- Produces:
  - Function `diff_and_alert(current: list[Check], previous: dict[str, CheckState], webhook_url: str, reminder_min: int, *, now: datetime, systemic_threshold: int = 5, post_fn: Callable | None = None) -> dict[str, CheckState]` — returns the updated dict of CheckState for save_state. `post_fn` is an injectable POST function so tests don't need HTTP mocking here.

- [ ] **Step 4.1: Write failing tests**

Append to `tests/test_watchdog.py`:

```python
# ----- state machine: diff_and_alert -----


class FakePoster:
    """Records POSTed payloads instead of hitting the network."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, webhook_url, payload, **kwargs):
        self.calls.append(payload)
        return True


def test_first_run_no_alerts_fire() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    checks = [watchdog.Check(name="supervisord", status="ok")]
    poster = FakePoster()
    updated = watchdog.diff_and_alert(
        checks, previous={}, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert poster.calls == []
    assert updated["supervisord"].status == "ok"


def test_ok_to_ok_no_alert() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(status="ok", since=now.isoformat())}
    checks = [watchdog.Check(name="x", status="ok")]
    poster = FakePoster()
    watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert poster.calls == []


def test_ok_to_fail_fires_down_alert() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(status="ok", since=now.isoformat())}
    checks = [watchdog.Check(name="x", status="fail", detail="broken")]
    poster = FakePoster()
    updated = watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert len(poster.calls) == 1
    assert "DOWN" in poster.calls[0]["text"].upper()
    assert updated["x"].status == "fail"
    assert updated["x"].last_alerted is not None


def test_fail_to_fail_within_reminder_window_no_alert() -> None:
    now = datetime(2026, 7, 16, 12, 5, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(
        status="fail",
        since=(now - timedelta(minutes=5)).isoformat(),
        last_alerted=(now - timedelta(minutes=5)).isoformat(),
        detail="broken",
    )}
    checks = [watchdog.Check(name="x", status="fail", detail="broken")]
    poster = FakePoster()
    watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert poster.calls == []


def test_fail_to_fail_past_reminder_fires_still_down() -> None:
    now = datetime(2026, 7, 16, 12, 32, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(
        status="fail",
        since=(now - timedelta(minutes=32)).isoformat(),
        last_alerted=(now - timedelta(minutes=32)).isoformat(),
        detail="broken",
    )}
    checks = [watchdog.Check(name="x", status="fail", detail="broken")]
    poster = FakePoster()
    updated = watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert len(poster.calls) == 1
    assert "STILL" in poster.calls[0]["text"].upper()
    # last_alerted must have advanced.
    assert updated["x"].last_alerted == now.isoformat()


def test_fail_to_ok_fires_recovered_and_clears_last_alerted() -> None:
    now = datetime(2026, 7, 16, 12, 47, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(
        status="fail",
        since=(now - timedelta(minutes=42)).isoformat(),
        last_alerted=(now - timedelta(minutes=30)).isoformat(),
        detail="broken",
    )}
    checks = [watchdog.Check(name="x", status="ok")]
    poster = FakePoster()
    updated = watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert len(poster.calls) == 1
    assert "RECOVERED" in poster.calls[0]["text"].upper()
    assert updated["x"].status == "ok"
    assert updated["x"].last_alerted is None


def test_warn_to_fail_fires_down_alert() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    prev = {"x": watchdog.CheckState(
        status="warn",
        since=(now - timedelta(hours=1)).isoformat(),
    )}
    checks = [watchdog.Check(name="x", status="fail", detail="crossed threshold")]
    poster = FakePoster()
    watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster,
    )
    assert len(poster.calls) == 1


def test_systemic_outage_coalesces_into_one_alert() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    prev = {}  # everything currently ok
    checks = [
        watchdog.Check(name=f"c{i}", status="fail", detail=f"detail-{i}")
        for i in range(7)
    ]
    poster = FakePoster()
    watchdog.diff_and_alert(
        checks, previous=prev, webhook_url="w", reminder_min=30,
        now=now, post_fn=poster, systemic_threshold=5,
    )
    # 7 fails → 1 systemic alert, not 7 down alerts.
    assert len(poster.calls) == 1
    assert "SYSTEMIC" in poster.calls[0]["text"].upper()
```

- [ ] **Step 4.2: Verify tests fail**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: previous 29 pass, 8 new fail.

- [ ] **Step 4.3: Implement diff_and_alert**

Append to `src/verbatim/watchdog.py`:

```python
# ----- state machine -----


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
    """Compare current check results to previous state, dispatch alerts on
    transitions or stuck-in-fail reminders, and return the updated state dict
    for save_state to persist. Returns the updated state — does NOT save it.
    Set post_fn to a fake in tests; defaults to post_webhook.
    """
    post = post_fn or post_webhook
    updated: dict[str, CheckState] = {}
    down_transitions: list[Check] = []
    reminders: list[tuple[Check, CheckState]] = []
    recoveries: list[tuple[Check, CheckState]] = []

    for check in current:
        old = previous.get(check.name)
        if old is None:
            # First-run: seed but never alert.
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

    # Dispatch. Systemic coalescing applies only to same-tick down transitions.
    if len(down_transitions) >= systemic_threshold:
        post(webhook_url, render_systemic_alert(down_transitions))
    else:
        for check in down_transitions:
            post(webhook_url, render_alert(
                check, previous.get(check.name), now=now, kind="down",
            ))

    for check, old in recoveries:
        post(webhook_url, render_alert(check, old, now=now, kind="recovered"))

    for check, old in reminders:
        post(webhook_url, render_alert(check, old, now=now, kind="reminder"))

    return updated
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `37 passed`.

- [ ] **Step 4.5: Ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/ --fix
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: ruff clean, 37 tests pass.

- [ ] **Step 4.6: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/watchdog.py tests/test_watchdog.py
git commit -m "watchdog: state machine — transitions + reminders + systemic coalescing

diff_and_alert compares current checks vs persisted state, dispatches
one alert per transition (ok→fail, fail→ok) or stuck-in-fail reminder
(≥30 min since last_alerted). Coalesces into one 'SYSTEMIC OUTAGE'
alert when ≥5 checks fail in the same tick.

Post function is injectable so tests use a fake in-memory recorder
instead of the real webhook path.

8 new tests. Full watchdog suite: 37. Ruff clean."
```

---

## Task 5: Main orchestrator + config + flock

**Files:**
- Modify: `src/verbatim/watchdog.py` — append `run_once()` + config loading + main() entry
- Modify: `tests/test_watchdog.py` — append integration tests

**Interfaces:**
- Consumes: `load_state`, `save_state`, all `check_*`, `safe_check`, `diff_and_alert` (previous tasks)
- Produces:
  - Function `run_once(*, config: WatchdogConfig, state_path: Path, now: datetime | None = None, post_fn: Callable | None = None) -> dict[str, CheckState]` — the core orchestrator. Loads state, runs all checks, calls diff_and_alert, saves state, returns the new state (for tests to assert on). No flock — that's main()'s job.
  - Dataclass `WatchdogConfig` with fields `webhook_url: str`, `reminder_min: int`, `silence_hours: int`, `silence_fail_hours: int`, `bot_token: str | None`, `anthropic_api_key: str | None`, `db_path: Path`, `daemons: list[str]`.
  - Function `load_config_from_env() -> WatchdogConfig` — reads env vars, raises SystemExit(1) with a clear message if VERBATIM_WATCHDOG_WEBHOOK_URL missing.
  - Function `main(*, dry_run: bool = False, send_canary: bool = False) -> int` — the entry point. Acquires flock; returns 0 on success, non-zero on failure. Called by the Typer subcommand.

- [ ] **Step 5.1: Write failing tests**

Append to `tests/test_watchdog.py`:

```python
# ----- run_once + config -----


def test_load_config_missing_webhook_exits(monkeypatch, capsys) -> None:
    monkeypatch.delenv("VERBATIM_WATCHDOG_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        watchdog.load_config_from_env()
    assert exc.value.code == 1
    assert "VERBATIM_WATCHDOG_WEBHOOK_URL" in capsys.readouterr().err


def test_load_config_uses_defaults(monkeypatch) -> None:
    monkeypatch.setenv("VERBATIM_WATCHDOG_WEBHOOK_URL", "https://hooks/x")
    monkeypatch.setenv("VERBATIM_DB_PATH", "/tmp/state.db")
    monkeypatch.delenv("VERBATIM_WATCHDOG_REMINDER_MIN", raising=False)
    monkeypatch.delenv("VERBATIM_WATCHDOG_SILENCE_HOURS", raising=False)
    monkeypatch.delenv("VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS", raising=False)
    cfg = watchdog.load_config_from_env()
    assert cfg.webhook_url == "https://hooks/x"
    assert cfg.reminder_min == 30
    assert cfg.silence_hours == 24
    assert cfg.silence_fail_hours == 48


def test_run_once_writes_state_and_updates_canary(tmp_path, monkeypatch) -> None:
    """run_once should always update the canary + save state, even if no checks alert."""
    state_path = tmp_path / "state.json"
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE entities (id TEXT, created_at TEXT)")
    conn.commit()
    conn.close()

    # Mock all subprocess + httpx calls so run_once returns "ok" for everything.
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "RUNNING pid 1"})(),
    )
    cfg = watchdog.WatchdogConfig(
        webhook_url="https://hooks/x",
        reminder_min=30,
        silence_hours=24,
        silence_fail_hours=48,
        bot_token=None,           # skip slack check
        anthropic_api_key=None,   # skip anthropic check
        db_path=db_path,
        daemons=["verbatim-watch"],
    )
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    poster = FakePoster()
    watchdog.run_once(
        config=cfg, state_path=state_path, now=now, post_fn=poster,
    )
    assert state_path.exists()
    loaded = json.loads(state_path.read_text())
    assert loaded["canary"] == now.isoformat()
```

- [ ] **Step 5.2: Verify tests fail**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: previous 37 pass, 3 new fail.

- [ ] **Step 5.3: Implement config + run_once + main**

Append to `src/verbatim/watchdog.py`:

```python
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
            lambda: check_slack_api(config.bot_token),
            name="slack-api",
        ))
    if config.anthropic_api_key:
        checks.append(safe_check(
            lambda: check_anthropic_api(config.anthropic_api_key),
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

    updated = diff_and_alert(
        checks, previous,
        webhook_url=config.webhook_url,
        reminder_min=config.reminder_min,
        now=now,
        post_fn=post_fn,
    )
    save_state(state_path, updated, canary=now.isoformat())
    return updated


# ----- main entry (flock + orchestration) -----


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
    import fcntl
    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            f"[{_now_iso()}] watchdog: already running (lock held). exiting.",
            file=sys.stderr,
        )
        return 0

    try:
        if dry_run:
            post_fn = _dry_run_poster
        else:
            post_fn = None
        run_once(config=config, post_fn=post_fn)
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()

    return 0


def _dry_run_poster(webhook_url: str, payload: dict[str, str], **kwargs) -> bool:
    print(f"DRY RUN — would POST to {webhook_url}:")
    print(json.dumps(payload, indent=2))
    return True
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: `40 passed`.

- [ ] **Step 5.5: Ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/ --fix
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/test_watchdog.py -q
```
Expected: ruff clean, 40 tests pass.

- [ ] **Step 5.6: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/watchdog.py tests/test_watchdog.py
git commit -m "watchdog: config + run_once orchestrator + main() with flock

WatchdogConfig dataclass, load_config_from_env with defaults + missing-
webhook exit(1), run_once ties load_state → all checks → diff_and_alert
→ save_state (with canary), main acquires an fcntl flock so overlapping
cron ticks don't collide.

send_canary flag posts a hello message for deploy-time smoke testing;
dry_run prints what would be sent without POSTing.

3 new tests. Full watchdog suite: 40. Ruff clean."
```

---

## Task 6: Typer CLI subcommand

**Files:**
- Modify: `src/verbatim/cli.py` — add `verbatim watchdog` command near the other `@app.command` definitions (after `version` command around line 1425, before `# ----- init` section)

**Interfaces:**
- Consumes: `watchdog.main` (Task 5)
- Produces: user-visible `verbatim watchdog [--dry-run] [--send-canary]` command

- [ ] **Step 6.1: Add the Typer command to cli.py**

Find the `version` command in `src/verbatim/cli.py`:

```bash
grep -n "^def version\|^@app.command()" /Users/qatadaha/Coding/verbatim/src/verbatim/cli.py | head -5
```

Add just after the `version()` function:

```python
@app.command(name="watchdog")
def watchdog_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Run all checks + print what would be alerted, but don't POST or save state.",
    ),
    send_canary: bool = typer.Option(
        False, "--send-canary",
        help="Post a 'hello from watchdog' message to the webhook and exit. "
             "Use at deploy time to verify the wire actually reaches you.",
    ),
) -> None:
    """Run one health-check tick — alerts on daemon deaths, service failures,
    and data-flow silence via a Slack Incoming Webhook.

    Designed to be invoked by cron every 2 minutes. Independent of the
    supervisord-managed daemons it monitors.
    """
    from . import watchdog as watchdog_lib
    raise typer.Exit(code=watchdog_lib.main(
        dry_run=dry_run, send_canary=send_canary,
    ))
```

- [ ] **Step 6.2: Smoke-check the command loads**

```bash
/Users/qatadaha/Coding/.venv/bin/verbatim watchdog --help
```
Expected: help text prints (no import error). Should mention `--dry-run` and `--send-canary`.

- [ ] **Step 6.3: Full test suite check (nothing regressed)**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/ -q
```
Expected: existing tests pass + all watchdog tests pass. Total should be ~511 (471 existing + 40 new).

- [ ] **Step 6.4: Ruff**

```bash
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/
```
Expected: `All checks passed!`

- [ ] **Step 6.5: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add src/verbatim/cli.py
git commit -m "watchdog: add \`verbatim watchdog\` Typer subcommand

Wires watchdog.main() into the CLI. Supports --dry-run (print payloads
without POSTing) and --send-canary (deploy-time smoke test).

Total test count now 511. Ruff clean."
```

---

## Task 7: Version bump + CHANGELOG

**Files:**
- Modify: `pyproject.toml:7` — `version = "0.13.2"` → `version = "0.14.0"`
- Modify: `src/verbatim/__init__.py:3` — `__version__ = "0.13.2"` → `__version__ = "0.14.0"`
- Modify: `docs/index.html:354` — `v0.13.2` → `v0.14.0`
- Modify: `CHANGELOG.md` — add new entry at the top

**Interfaces:** No code interfaces — just release plumbing.

- [ ] **Step 7.1: Bump versions**

```bash
cd /Users/qatadaha/Coding/verbatim
sed -i '' 's/version = "0.13.2"/version = "0.14.0"/' pyproject.toml
sed -i '' 's/__version__ = "0.13.2"/__version__ = "0.14.0"/' src/verbatim/__init__.py
sed -i '' 's/v0.13.2 · open source · MIT/v0.14.0 · open source · MIT/' docs/index.html
grep -n "0.14.0" pyproject.toml src/verbatim/__init__.py docs/index.html
```
Expected: one match per file, all showing `0.14.0`.

- [ ] **Step 7.2: Add CHANGELOG entry**

Prepend to `CHANGELOG.md` (after the top `# Changelog` header, before `## [0.13.2]`):

```markdown
## [0.14.0] — 2026-07-16

### Added — Watchdog observability

A new cron-driven watchdog detects Verbatim's silent failures and DMs the
operator via a Slack Incoming Webhook, independent of the daemons being
monitored. Motivated by the 2026-07-14 supervisord silent-death incident
that stayed invisible until a user hit the timeout.

Runs every 2 minutes via cron. Probes seven signals:
- `supervisord` alive (pgrep)
- Each of the three daemons in RUNNING state (`supervisorctl status`)
- Slack API reachable (`auth.test` returns ok)
- Anthropic API not credit-exhausted (specifically catches the 400/402
  "credit balance is too low" class we hit during the marketing backfill)
- DB writes freshness — `warn` at 24h silence, `fail` at 48h
- Watchdog self-witness (canary timestamp in state.json)

State-transition alerting: one alert on ok→fail, one on fail→ok, and a
"still down" reminder every 30 minutes while stuck. Simultaneous failure
of ≥5 checks in the same tick coalesces into one "systemic outage"
alert instead of spamming individual ones.

Alert transport is a Slack Incoming Webhook (not the bot daemon), so when
Verbatim is broken the alert path is still intact. Configuration lives in
`.env`:

```
VERBATIM_WATCHDOG_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
VERBATIM_WATCHDOG_REMINDER_MIN=30
VERBATIM_WATCHDOG_SILENCE_HOURS=24
VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS=48
```

New CLI: `verbatim watchdog` (run one tick), `verbatim watchdog --dry-run`
(print what would be sent without POSTing), `verbatim watchdog
--send-canary` (post a hello message — used once at deploy time to prove
the wire reaches you).

### Tests
40 new tests in `tests/test_watchdog.py` cover state I/O, each check, the
state machine (transitions, reminders, systemic coalescing), alert
rendering, webhook retry, and end-to-end run_once. Full suite: 511
passed.

### Why
Verbatim previously had no path for telling its operator that Verbatim
itself was broken. The 2026-07-14 incident sat undetected for hours
because the failure was silent. This closes that class.

```

- [ ] **Step 7.3: Full validation before commit**

```bash
/Users/qatadaha/Coding/.venv/bin/python -m pytest tests/ -q
/Users/qatadaha/Coding/.venv/bin/ruff check src/ tests/
/Users/qatadaha/Coding/.venv/bin/python -m build 2>&1 | tail -3
/Users/qatadaha/Coding/.venv/bin/python -m twine check dist/*
```
Expected: 511 tests pass, ruff clean, build succeeds with `verbatim_ai-0.14.0-py3-none-any.whl`, twine PASSED.

- [ ] **Step 7.4: Commit**

```bash
cd /Users/qatadaha/Coding/verbatim
git add pyproject.toml src/verbatim/__init__.py docs/index.html CHANGELOG.md
git commit -m "v0.14.0: watchdog observability

New cron-driven health watchdog + Slack Incoming Webhook alerting.
Independent of the daemons it monitors, so alerts fire even during a
total Verbatim outage. Motivated by the 2026-07-14 supervisord silent-
death incident.

40 new tests in tests/test_watchdog.py. Full suite: 511 passed. Ruff
clean. Build + twine check clean."
```

- [ ] **Step 7.5: Push + tag**

```bash
cd /Users/qatadaha/Coding/verbatim
git push
git tag v0.14.0
git push origin v0.14.0
```
Expected: both pushes succeed; PyPI release workflow starts async.

---

## Task 8: Deploy to pod + smoke test

**Files:** No file changes — this is deployment only.

**Interfaces:** Uses everything from the previous tasks + Qat's operator setup on the pod (existing `.env`, `run.sh`, `supervisord`, and cron infrastructure).

**Prerequisite for the operator (Qat):**
Create a Slack Incoming Webhook — https://api.slack.com/apps → Verbatim → Incoming Webhooks → Add New Webhook to Workspace → pick a DM to yourself or a `#verbatim-ops` channel → copy the URL. It looks like `https://hooks.slack.com/services/T.../B.../...`. Set aside for step 8.3.

- [ ] **Step 8.1: Build wheel + scp to pod**

```bash
cd /Users/qatadaha/Coding/verbatim
/Users/qatadaha/Coding/.venv/bin/python -m build --wheel 2>&1 | tail -3
scp -P 27009 -i ~/.ssh/id_ed25519 -q dist/verbatim_ai-0.14.0-py3-none-any.whl root@103.196.86.88:/tmp/
rm -rf dist build
```
Expected: `Successfully built verbatim_ai-0.14.0-py3-none-any.whl`. scp exits 0.

- [ ] **Step 8.2: Install wheel on pod**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
/opt/verbatim/venv/bin/pip install --quiet --force-reinstall --no-deps /tmp/verbatim_ai-0.14.0-py3-none-any.whl 2>&1 | tail -3
/opt/verbatim/run.sh version
'
```
Expected: `verbatim 0.14.0`.

- [ ] **Step 8.3: Add webhook + threshold config to pod .env**

Operator (Qat) runs this on their laptop (webhook URL in local shell only, never in chat). Replace `<YOUR_WEBHOOK_URL>` with the URL created in the prerequisite:

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 "cat >> /opt/verbatim/.env <<EOF
VERBATIM_WATCHDOG_WEBHOOK_URL=<YOUR_WEBHOOK_URL>
VERBATIM_WATCHDOG_REMINDER_MIN=30
VERBATIM_WATCHDOG_SILENCE_HOURS=24
VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS=48
EOF"
```

- [ ] **Step 8.4: Verify config was written correctly**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
echo "--- .env layout (values masked) ---"
awk -F= "{ printf \"  %-40s = %d chars (prefix %s...)\\n\", \$1, length(\$0)-length(\$1)-1, substr(\$2,1,5) }" /opt/verbatim/.env
'
```
Expected: shows `VERBATIM_WATCHDOG_WEBHOOK_URL` with >30 chars, prefix `https`. Confirms operator pasted a real URL, not a placeholder.

- [ ] **Step 8.5: Live canary test — verify webhook actually reaches Slack**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
mkdir -p /opt/verbatim/health
/opt/verbatim/run.sh watchdog --send-canary
'
```
Expected: `canary sent` on stderr. Operator confirms the "verbatim watchdog canary" message shows up in Slack DM/channel. If it doesn't arrive, webhook URL is wrong — fix + retry.

- [ ] **Step 8.6: Install cron entry**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
crontab -l 2>/dev/null | grep -v "run.sh watchdog" > /tmp/newcron || true
cat >> /tmp/newcron <<CRON
*/2 * * * * /opt/verbatim/run.sh watchdog >> /opt/verbatim/logs/watchdog.log 2>&1
CRON
crontab /tmp/newcron
rm /tmp/newcron
crontab -l
'
```
Expected: `crontab -l` shows the new entry alongside the existing keepalive entry.

- [ ] **Step 8.7: Wait ≤3 minutes for cron to fire, verify state.json created**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
sleep 180
ls -la /opt/verbatim/health/
cat /opt/verbatim/health/state.json | head -30
tail -5 /opt/verbatim/logs/watchdog.log
'
```
Expected: `state.json` exists, contains a `checks` object with 7 entries (all `"status": "ok"` at first run), and a `canary` field with a recent timestamp.

- [ ] **Step 8.8: Live down-alert test — kill supervisord + confirm alert lands**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
echo "--- killing supervisord ---"
pkill -x supervisord
date -u --iso-8601=seconds
echo "--- waiting 4 min for watchdog to detect + keepalive to restart ---"
sleep 240
echo
echo "--- post-recovery status ---"
supervisorctl status
echo
echo "--- watchdog log ---"
tail -20 /opt/verbatim/logs/watchdog.log
echo
echo "--- state.json ---"
cat /opt/verbatim/health/state.json | head -40
'
```
Expected:
- Operator sees a `:rotating_light: supervisord is DOWN` alert in Slack within ~2 minutes
- Then `:white_check_mark: supervisord RECOVERED` when the cron keepalive brings it back
- `supervisorctl status` shows all three daemons RUNNING again
- state.json's `supervisord` check shows `status: "ok"` and `last_alerted: null` (cleared on recovery)

- [ ] **Step 8.9: Clean up the wheel**

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 'rm /tmp/verbatim_ai-0.14.0-py3-none-any.whl'
```

- [ ] **Step 8.10: Report result to Qat**

Include:
- `verbatim 0.14.0` running on pod
- Webhook fires end-to-end (canary + down + recovered all seen in Slack)
- Cron entry installed alongside the existing keepalive
- Max alert latency: `state.json` last check timestamp minus `date -u`

If any step fails, roll back:

```bash
ssh -p 27009 -i ~/.ssh/id_ed25519 root@103.196.86.88 '
crontab -l | grep -v "run.sh watchdog" | crontab -
'
```
(Reverts the crontab. The wheel + code stay installed but the watchdog stops firing.)

---

## Self-Review

**Spec coverage check** (against `docs/superpowers/specs/2026-07-16-watchdog-observability-design.md`):

- ✅ §1 Architecture (cron-driven, independent transport, stateful) → Tasks 1, 5, 6
- ✅ §2.1 `watchdog.py` module → Tasks 1–5
- ✅ §2.2 `state.json` schema → Task 1 (persisted schema matches spec §2.2)
- ✅ §2.3 `.env` additions → Task 5 (config loader) + Task 8 (deploy)
- ✅ §2.4 Crontab entry → Task 8.6
- ✅ §2.5 `verbatim watchdog` subcommand → Task 6
- ✅ §2.6 test_watchdog.py → Tasks 1–5 (40 tests total)
- ✅ §3.1 Per-check state machine → Task 4
- ✅ §3.2 Cron tick flow → Task 5 (run_once) + main()'s flock (Task 5)
- ✅ §3.3 Alert payload examples → Task 3 (render_alert)
- ✅ §3.4 Silence detection subtlety (24h warn / 48h fail) → Task 2 (check_db_silence)
- ✅ §3.5 Configuration knobs → Task 5 (load_config_from_env defaults)
- ✅ §4.1 Missing webhook URL exits → Task 5 (load_config_from_env)
- ✅ §4.2 Corrupted state.json → Task 1 (load_state)
- ✅ §4.3 State file write failure — Task 1 (save_state exceptions propagate to caller; cron records stderr in log)
- ✅ §4.4 Individual check throws → Task 2 (safe_check)
- ✅ §4.5 Webhook POST fails → Task 3 (post_webhook retry + _log_dropped_alert)
- ✅ §4.6 Systemic rate limiting → Task 4 (diff_and_alert coalescing at threshold 5)
- ✅ §4.7 Canary → Task 2 (check_canary_freshness) + Task 5 (run_once updates canary)
- ✅ §4.8 flock → Task 5 (main)
- ✅ §4.9 Clock skew → all datetimes are ISO-8601 UTC
- ✅ §5 Testing → Tasks 1–5 write 40 tests total (spec said ~24; we're slightly over because state I/O got more coverage)
- ✅ §5.5 Live integration → Task 8.5 (send_canary)
- ✅ §5.6 Post-deploy smoke test → Tasks 8.5, 8.7, 8.8

**Placeholder scan:** Every code block is complete. No "TBD" / "TODO". The one operator-side placeholder is `<YOUR_WEBHOOK_URL>` in Task 8.3, which is by design — the URL is a secret and only the operator has it.

**Type consistency:** `Check`, `CheckState`, `WatchdogConfig` names used consistently across tasks. `render_alert(check, previous, *, now, kind)` signature matches its consumers in `diff_and_alert`. `post_fn` optional Callable used the same shape everywhere.

**Scope check:** Single feature, ~350 LOC + 40 tests + config + rollout. Fits one plan.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-watchdog-observability.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each of the 8 tasks becomes its own scoped agent invocation with a clean review gate.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints for review.

Which approach?
