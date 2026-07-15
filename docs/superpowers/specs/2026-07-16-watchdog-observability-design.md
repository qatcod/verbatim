# Watchdog Observability — Design Spec

**Date:** 2026-07-16
**Status:** Draft, pending final approval
**Target version:** v0.14.0

## Context

Verbatim runs three daemons on a RunPod pod: `verbatim-watch`, `verbatim-slack-bot`, `verbatim-daily`, all managed by `supervisord`. A `cron` keepalive (shipped 2026-07-14) restarts `supervisord` within 60 seconds if it dies.

**The gap this spec closes:** silent failures aren't detected until a user hits a symptom. On 2026-07-14, `supervisord` died and the `/verbatim` slash command timed out for hours before anyone noticed — because Jason ran a command and got a Slack "app did not respond" error. There is currently no path by which Verbatim tells its operator that Verbatim is broken.

## Goal

Verbatim detects its own failures and DMs the operator via a Slack Incoming Webhook, independent of the daemons being monitored. Alert cadence is *state-transition based* (not per-tick), with reminders every 30 minutes while an incident is ongoing, and recovery alerts when it clears.

## Non-goals

- Metrics dashboards, time-series storage, historical analysis (Prometheus/Grafana is out of scope)
- Multi-recipient / on-call rotation (single webhook, single destination)
- Log aggregation / structured log shipping
- Auto-remediation beyond what the existing cron keepalive already provides
- Alerting for the OSS-installed pattern (multi-user, hosted). This design targets Data443's single-pod install.

## Success criteria

1. Within ≤4 minutes of any of the three daemons entering a non-RUNNING state, a Slack DM lands showing what broke and how long it has been broken.
2. Within ≤2 minutes of the same daemon returning to RUNNING, a "recovered" DM lands.
3. If the failure persists, a "still down" reminder lands every 30 minutes.
4. During a systemic outage where >5 checks fail simultaneously, a single consolidated alert fires — not one alert per check.
5. The watchdog itself never fails silently: bad config exits non-zero, corrupted state file is logged and reset, individual check exceptions become an alert about the broken check, and a canary check detects if the watchdog stops running.

---

## 1 — Architecture

A fifth artifact on the pod: a self-contained health watchdog, cron-driven, decoupled from `supervisord` and from all three Verbatim daemons. It is the only piece of the stack whose job is to notice when the others are broken.

```
                   ┌──────────────────────────────────┐
                   │              cron                │
                   │   */2 * * * * /opt/verbatim/     │
                   │              run.sh watchdog     │
                   └──────────────┬───────────────────┘
                                  │
                                  ▼
        ┌──────────────────────────────────────────────┐
        │        /opt/verbatim/watchdog.py             │
        │                                              │
        │  1. run checks:                              │
        │     • supervisord alive                      │
        │     • verbatim-watch/slack-bot/daily RUNNING │
        │     • Slack auth.test 200                    │
        │     • Anthropic /messages returns non-402    │
        │     • DB writes in last 24h (silence)        │
        │                                              │
        │  2. diff vs /opt/verbatim/health/state.json  │
        │  3. on transition or 30-min re-alert:        │
        │       POST → Slack Incoming Webhook          │
        │  4. write current state → state.json         │
        └──────────────────────────────────────────────┘
                                  │
                                  ▼
                        ┌───────────────────┐
                        │  Slack Webhook    │
                        │  → operator DM    │
                        └───────────────────┘
```

**Key properties:**

- **Independent transport.** Alerts POST directly to a Slack Incoming Webhook via `httpx`. No bot daemon, no MCP, no `supervisord` in the alert path. When Verbatim is broken, the alert path is still intact.
- **Cron cadence, not always-on process.** Fires every 2 minutes, exits. Nothing to keepalive; nothing to die.
- **Stateful across invocations** via a JSON file at `/opt/verbatim/health/state.json`. This is how "alert once on transition, not once per check" works.
- **Config in `.env`**, not code. Webhook URL and threshold knobs live alongside the existing tokens.

Boundary is clean: watchdog reads external systems (files, sockets, APIs), writes state file + webhook. Doesn't share state with the daemons it monitors. Testable in isolation.

---

## 2 — Components

Six concrete artifacts. Each has one job.

### 2.1 `/opt/verbatim/watchdog.py`

~200 LOC Python, no new dependencies beyond `httpx` (already in the venv).

- `main()` — orchestrator: acquire lock, load state, run checks, diff, alert, save state
- `check_*()` functions — one per signal. Each returns `Check(name: str, status: Literal["ok", "warn", "fail"], detail: str)`
- `diff_and_alert(current: dict, previous: dict) -> None` — compare, decide, dispatch
- `post_webhook(payload: dict) -> bool` — pure HTTP POST, retries once on failure

### 2.2 `/opt/verbatim/health/state.json`

Dedup state. Schema:

```json
{
  "checks": {
    "supervisord":        {"status": "ok",   "since": "2026-07-16T12:00:00Z", "last_alerted": null},
    "verbatim-watch":     {"status": "ok",   "since": "2026-07-16T12:00:00Z", "last_alerted": null},
    "verbatim-slack-bot": {"status": "fail", "since": "2026-07-16T12:14:03Z", "last_alerted": "2026-07-16T12:16:00Z", "detail": "FATAL"},
    "verbatim-daily":     {"status": "ok",   ...},
    "slack-api":          {"status": "ok",   ...},
    "anthropic-api":      {"status": "ok",   ...},
    "db-silence":         {"status": "warn", ...}
  },
  "canary": {
    "last_run": "2026-07-16T13:02:00Z"
  }
}
```

The `since` field tracks "in this state since when." `last_alerted` tracks the 30-minute re-alert timer. `canary.last_run` is the self-witness for the "watchdog watches watchdog" pattern.

### 2.3 `.env` additions

Writes to `/opt/verbatim/.env` alongside the existing tokens:

```
VERBATIM_WATCHDOG_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
VERBATIM_WATCHDOG_SILENCE_HOURS=24
VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS=48
VERBATIM_WATCHDOG_REMINDER_MIN=30
```

### 2.4 Crontab entry

Add to the existing crontab:

```
*/2 * * * * /opt/verbatim/run.sh watchdog >> /opt/verbatim/logs/watchdog.log 2>&1
```

Reuses `run.sh` so watchdog inherits the same `.env` loading pattern the daemons use.

### 2.5 `verbatim watchdog` subcommand

New Typer command in `src/verbatim/cli.py`. Delegates to `src/verbatim/watchdog.py`. This is what makes the whole thing part of the Verbatim CLI — discoverable, testable via `verbatim watchdog --dry-run` (print what would fire without sending), and manually invokable for smoke testing.

### 2.6 `tests/test_watchdog.py`

~25 new tests covering per-check units, the state machine, error paths, alert payload rendering. See Section 5.

---

## 3 — Data flow (the state machine)

Every check has a status of `ok` / `warn` / `fail`. Alerts fire on **transitions between states**, plus a **reminder timer** while stuck in `fail`.

### 3.1 Per-check state machine

```
                         ┌─────────────────────────┐
                         │                         │
                         ▼                         │
                   ┌──────────┐                    │
     first run ──▶ │    ok    │◀──── fail→ok ──────┼─── ALERT: "recovered"
                   └────┬─────┘                    │
                        │                          │
                     ok→fail                       │
                        │                          │
                        ▼                          │
                   ┌──────────┐                    │
                   │   fail   │────── stays fail   │
                   └────┬─────┘   for ≥ 30 min ────┼─── ALERT: "still down"
                        │                          │
                        └──────────────────────────┘
                              ALERT: "went down"
```

### 3.2 Cron tick flow (every 2 minutes)

```
1. LOCK   flock /opt/verbatim/health/watchdog.lock (exit if already held)
2. LOAD   state.json  (or bootstrap empty if missing / corrupted)
3. RUN    all check_*() functions — collect results, wrap each in try/except
4. FOR each check:
     old = state["checks"][name] or {status: "ok", since: now, last_alerted: null}
     new = current result
     
     transitioned  = (old.status != new.status)
     stuck_and_stale = (
       new.status == "fail"
       AND old.last_alerted is not None
       AND now - old.last_alerted ≥ REMINDER_MIN
     )
     
     IF transitioned:
       send_alert(check, old.status → new.status)
       new.last_alerted = now if new.status == "fail" else null
     ELIF stuck_and_stale:
       send_alert(check, "still down for {duration}")
       new.last_alerted = now
     
     new.since = old.since if not transitioned else now
     state["checks"][name] = new

5. IF > 5 checks just transitioned to "fail" simultaneously:
     COALESCE those into one "systemic failure" alert (see §4.6)

6. UPDATE state.canary.last_run = now
7. SAVE   state.json (write to .tmp, then atomic rename)
8. RELEASE lock
```

### 3.3 Alert payload examples

**Down alert (transition ok → fail):**

```
🚨  verbatim-slack-bot is DOWN
    down since:    2026-07-16 12:14 UTC (0h 4m ago)
    detail:        supervisorctl reports FATAL — last log:
                   "Bot token required. Set $SLACK_BOT_TOKEN"
    fix:           ssh -p 27009 ... 'supervisorctl restart verbatim-slack-bot'
```

**Recovery alert (fail → ok):**

```
✅  verbatim-slack-bot RECOVERED
    down for:      42 min
    now:           RUNNING pid 1247
```

**Reminder alert (fail → fail, 30+ min since last):**

```
🚨  verbatim-slack-bot STILL DOWN
    down for:      1h 12m
    last check:    2026-07-16 13:26 UTC
    detail:        (same as first alert)
```

**Systemic outage alert (>5 checks fail simultaneously):**

```
🚨  SYSTEMIC OUTAGE — 7 checks failed in this tick
    supervisord, verbatim-watch, verbatim-slack-bot,
    verbatim-daily, slack-api, anthropic-api, db-silence
    fix:           investigate pod networking / power
```

### 3.4 Silence detection subtlety

For "no DB writes in 24h across all watched channels":

- 0 – 24h since last entity insert → `ok`
- 24 – 48h → `warn` (a slow work day is normal; alert but not urgent)
- \>48h → `fail` (two silent days on an active team is a real dead-pipeline signal)

Reason: production teams have quiet Sundays. Alerting `fail` on 24h would fire every Monday morning. 48h is the shortest window that avoids that false positive.

### 3.5 Configuration knobs (`.env`)

- `VERBATIM_WATCHDOG_REMINDER_MIN=30` — how long between "still down" reminders
- `VERBATIM_WATCHDOG_SILENCE_HOURS=24` — silence threshold for `warn`
- `VERBATIM_WATCHDOG_SILENCE_FAIL_HOURS=48` — silence threshold for `fail`

Adjustable per instance without touching code.

---

## 4 — Error handling

The whole point is to catch silent failures elsewhere. A silent failure in the watchdog is the worst outcome possible. Every path either recovers gracefully or leaves a loud trail.

### 4.1 Missing webhook URL

Watchdog checks `VERBATIM_WATCHDOG_WEBHOOK_URL` on startup. If missing or empty, exits non-zero with `"ERROR: VERBATIM_WATCHDOG_WEBHOOK_URL not set in .env"` to stderr → cron's default mail path + `/opt/verbatim/logs/watchdog.log`.

Never silently no-ops. Prevents the pathological case where watchdog runs happily but alerts go nowhere.

### 4.2 Corrupted state file

On load failure (invalid JSON, permission denied, disk error): log the corruption to `watchdog.log`, treat as `{}` (start fresh). First subsequent tick will look like a first-run — no spurious "recovered" alerts because there was no prior "down" state to transition from. The loud log line ensures you can grep later.

### 4.3 State file write failure

Log to stderr and `watchdog.log`, exit non-zero. Next tick retries. Won't cause double-alerts because the transition already fired on this tick, but next tick will re-detect and re-fire, which is acceptable for a rare disk failure.

### 4.4 Individual check throws

Each `check_*()` wrapped in `try/except`:

```python
def safe_check(fn, name):
    try:
        return fn()
    except Exception as e:
        return Check(name=name, status="fail",
                     detail=f"check function raised: {type(e).__name__}: {e}")
```

A broken check produces an alert *about the broken check*, rather than crashing the whole watchdog.

### 4.5 Webhook POST fails

Retry once with 2s backoff. On second failure: log to `watchdog.log` with the payload that would have been sent. Do NOT update `last_alerted` — so next tick retries the alert. Alert eventually gets through if the outage is transient.

### 4.6 Systemic failure rate limiting

If >5 checks transition to `fail` on the same tick, coalesce into one systemic alert (see §3.3). Each check's `last_alerted` is still updated to `now` so individual reminders resume at 30 min. Prevents alert flood during a total outage.

### 4.7 Watchdog exception before any check runs

Import error, missing dependency, syntax error. Cron captures stderr → `watchdog.log`. Additionally: a **canary** field in state.json is updated on every successful tick. A separate check (`check_canary_freshness`) detects if `state.canary.last_run` is more than 15 minutes stale — meaning the watchdog itself has not run — and fires an alert.

This is the "watchdog watches watchdog" pattern. It only works if at least one successful tick has run recently enough for the *next* tick's canary check to be meaningful, so the first line of defense is still `flock` + persistent logs.

### 4.8 Concurrent invocation

If a check hangs (e.g., 3-minute Slack API timeout), the next cron tick would start before the current one finishes. Use `flock` on `/opt/verbatim/health/watchdog.lock` at the start of `main()`. Second invocation exits immediately with `"already running"` in the log. No double state updates, no torn writes.

### 4.9 Clock skew

All timestamps are ISO-8601 UTC (`datetime.now(timezone.utc)`). Reminder timer uses `now - last_alerted` in seconds. A 5-minute NTP correction shifts the timer by 5 minutes — annoying but not corrupting.

### 4.10 Design principle

*The watchdog never fails silently.* Every error path either alerts, logs to a persistent file that the next run inspects, or both. If a bug ever lets watchdog run 24 hours without alerting on a legitimate failure, the canary check catches it on tick 12,001.

---

## 5 — Testing

Verbatim's existing test surface is 471 pytest tests with `httpx.MockTransport` for HTTP mocking and in-memory SQLite for state. New tests follow the same patterns. All new tests live in `tests/test_watchdog.py`.

### 5.1 Per-check unit tests (7 tests)

Each mocks its external dependency and asserts `Check(name, status, detail)`:

- `check_supervisord_alive` — mock `subprocess.run("pgrep supervisord")` exit 0 vs 1
- `check_daemon` — mock `supervisorctl status verbatim-slack-bot` returning RUNNING / FATAL / STARTING / unknown-daemon
- `check_slack_api` — mock `httpx.post` returning `{"ok": True}` vs `{"ok": False, "error": "invalid_auth"}`
- `check_anthropic_api` — mock returning 200 vs 402 (credit exhaustion, the exact failure hit during marketing backfill)
- `check_db_silence` — seed a tmp SQLite with entities aged different amounts; assert ok → warn (24h) → fail (48h) boundaries
- `check_canary_freshness` — assert stale state.canary.last_run detected
- `check_webhook_reachable` — mock `httpx.head` on the webhook URL

### 5.2 State machine tests (8 tests)

No mocking of external systems. Drive `diff_and_alert()` directly with synthetic before/after states.

- First run — no state file → no alerts fire (baseline)
- ok → ok → no alert
- ok → fail → 1 alert ("went down")
- fail → fail, `last_alerted < REMINDER_MIN` → no alert
- fail → fail, `last_alerted ≥ REMINDER_MIN` → 1 alert ("still down"), `last_alerted` updated
- fail → ok → 1 alert ("recovered"), `last_alerted` cleared
- warn → fail → 1 alert (warn boundary crossed)
- Systemic failure (>5 simultaneous fail transitions) → 1 consolidated alert, not 6

### 5.3 Error path tests (6 tests)

- state.json missing → treated as `{}`, no crash
- state.json corrupted → logged, treated as `{}`, no double-alerts
- Webhook POST fails → retried once, then logged; `last_alerted` NOT updated
- Individual `check_*` throws → wrapped, produces `fail` with the exception as detail
- Missing `VERBATIM_WATCHDOG_WEBHOOK_URL` → exits non-zero with clear message
- Concurrent watchdog invocation → `flock` blocks second; second exits with "already running"

### 5.4 Alert payload rendering (3 tests)

- Down alert contains: check name, "down since", human duration ("4m ago"), fix hint
- Recovered alert contains: "recovered", "was down for", human duration
- Systemic alert contains: list of failed check names, no per-check spam

### 5.5 Live integration test

`verbatim watchdog --dry-run` prints what would be sent to stdout without POSTing.
`verbatim watchdog --send-canary` (guarded by explicit flag) POSTs a canary "hello from watchdog" message to the webhook. Used once at deploy time to prove the wire actually reaches the operator's phone.

### 5.6 Post-deploy smoke test

Before we call this done on the pod:

1. Deploy watchdog + confirm cron entry fires (grep `watchdog.log` for a fresh timestamp)
2. `kill $(pgrep -x supervisord)` → wait ≤ 4 min → confirm alert lands in Slack
3. `cron` restarts supervisord within 60s (existing keepalive) → confirm "recovered" alert lands
4. This is the only way to catch a bad webhook URL, a firewall rule, or a corrupt crontab — mocks can't.

### 5.7 Test count summary

~24 unit + state machine + error path + rendering tests. Full pytest suite goes from 471 → ~495.

---

## Rollout / deployment

1. Build v0.14.0 wheel locally
2. `scp` to pod
3. `pip install --force-reinstall --no-deps` (matches the pattern from v0.13.x deploys)
4. Add `VERBATIM_WATCHDOG_WEBHOOK_URL` + three threshold knobs to `/opt/verbatim/.env`
5. Create `/opt/verbatim/health/` directory
6. Add crontab entry `*/2 * * * * /opt/verbatim/run.sh watchdog >> /opt/verbatim/logs/watchdog.log 2>&1`
7. Run smoke test §5.6

The operator needs to create the Slack Incoming Webhook themselves (Slack app → Incoming Webhooks → Add New Webhook to Workspace → pick channel or DM → copy URL). ~5 minutes.

## Open questions

None. All key decisions locked during brainstorming:

- Transport: Slack Incoming Webhook (decided)
- Cadence: transitions + 30-min reminders + recovery (decided)
- Scope: daemons + service failures + silence detection (decided)
- Architecture: cron-driven, independent of supervisord (decided)

## Future work (out of scope for this spec)

- Metrics + Grafana dashboard (needs a separate spec if pursued)
- Multi-recipient / on-call rotation (config extension, small if needed)
- Extraction-quality signal (dismissal rate as a check) — logical next step but not necessary for the observability MVP
- Container-entrypoint supervisord (RunPod UI change, separate operational fix)
