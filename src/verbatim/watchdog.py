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
from dataclasses import asdict, dataclass
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
