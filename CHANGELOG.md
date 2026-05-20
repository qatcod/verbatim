# Changelog

All notable changes to Verbatim. This project follows [Semantic Versioning](https://semver.org/).

## [0.12.0] — 2026-05-21

### Added — Natural-language ask

`verbatim ask "what did we decide about the database?"` — query the state
graph in plain English instead of picking the right structured command.

New `verbatim.ask` module assembles the current open commitments,
decisions, questions, and blockers into a compact context block (each item
with its VRB-id and verbatim quote), hands it to the LLM with the question,
and returns a grounded answer.

**Grounding contract.** The system prompt forces the model to answer ONLY
from the provided state, cite `VRB-<id>` references, quote verbatim source
text, and say "not in the state" rather than invent an answer. Same
no-source-no-claim principle as the extractor.

**Backend.** Reuses the extractor's Anthropic/Ollama split — `--model
ollama:…` answers fully locally. Unlike extraction (forced tool call),
`ask` uses the plain chat endpoints for a free-text answer.

- **CLI**: `verbatim ask <question>` — prints the answer plus a footer
  (items considered · model · estimated cost).
- **Slack**: `/verbatim ask <question>` — the bot answers in-channel.
- **Web UI / MCP**: state was already queryable via MCP; `ask` makes the
  same capability available without an MCP client.

### Tests
- 7 new tests in `tests/test_ask.py`: state-context assembly (all four
  kinds, quotes, VRB-ids, empty DB), Ollama backend dispatch with mocked
  HTTP, empty-response error path, and the grounding-rule system prompt.

### Changed — landing page
The landing page got a substantial polish (no version bump of its own —
it ships continuously via GitHub Pages): a faithful HTML/CSS mockup of the
web UI, a "How it works" 3-step section, "Not another AI notetaker"
positioning, and a 6-question FAQ.

### Why
The structured queries (`query commitments`, `query overdue`, …) are
precise but you have to know they exist. `ask` is the zero-learning-curve
front door — and it's the same muscle the MCP server gives agents, now
available to humans at the CLI and in Slack.

## [0.11.2] — 2026-05-21

### Added — Contradiction detection

A team decides "use Postgres" in one meeting and "use SQLite" two weeks
later, on the same topic, and nobody connects the two. Verbatim holds both
decisions — `verbatim query contradictions` now surfaces the conflict.

New `verbatim.contradictions` module flags pairs of open decisions where
the **topics** are similar (rapidfuzz token-set ratio ≥ 75) but the
**outcomes** are not (ratio < 55). Same topic + same outcome is a
duplicate — reconciliation's job; same topic + different outcome is the
conflict worth a human's attention.

Thresholds are looser than reconciliation's 88 on purpose: a contradiction
is a prompt to look, not an automatic state change, so a few false
positives are cheap and a missed conflict is not. `--topic-threshold` /
`--outcome-threshold` tune the sensitivity.

- **CLI**: `verbatim query contradictions` — lists each conflicting pair
  with both outcomes and the topic-match score.
- **Web**: new `/contradictions` page, with a "Contradictions" sidebar
  entry and a live count.

### Tests
- 10 new tests in `tests/test_contradictions.py`: same-topic/different-
  outcome detection, no-flag for identical outcomes (duplicate) or
  unrelated topics, empty-outcome skipping, resolved-decision exclusion,
  threshold tuning, and the `/contradictions` web route.

### Why
This is the proactive layer's hardest-to-spot win. Overdue commitments and
stale items are visible if you look; a contradiction between two decisions
made weeks apart is invisible to everyone — no single person was in both
rooms. Catching it is exactly what a memory layer is *for*.

## [0.11.1] — 2026-05-21

### Added — Staleness detection

`verbatim query stale` flags open entities that have sat untouched too
long — no confirm / edit / reassign / dismiss activity since the cutoff
(default 30 days). These are items quietly rotting in the state graph: a
commitment nobody's looked at in two months is either done-and-forgotten
or genuinely dropped, and either way it should resurface.

`--days N` tunes the cutoff; `--kind` restricts to one entity kind. Each
row shows how many days the item has been idle. The audit log (v0.10.1) is
the activity signal — an entity with zero audit rows counts as untouched.

### Added — Auto-standup

`verbatim standup <person>` generates a standup-style status summary from
the state graph — no manual writing:

- **On the hook** — open commitments the person owns, deadline-annotated
  (overdue / due today / in N days).
- **Blocked** — blockers they own.
- **Open questions raised** — unanswered questions they asked.
- **Recently moved** — entities tied to them with a confirm / resolve /
  dismiss in the audit log within the last 7 days (`--recent-days` tunes
  the window).

Paste it straight into a standup channel. It's the person view (v0.10.0)
reframed as a report rather than a browse surface.

### Tests
- 9 new tests in `tests/test_proactive.py`: `stale_entities` (old +
  untouched flagged, recent excluded, recently-audited excluded, kind
  filter) and `standup` (all buckets collected, deadline annotation,
  audit-driven recently-moved, old-audit-ignored, unknown-person empty).

### Why
Staleness and standup are the second and third proactive features. Stale
detection catches the failure mode every team tool has — items that get
created and then silently abandoned. Auto-standup removes the busywork of
writing a status update when Verbatim already holds every fact it needs.

## [0.11.0] — 2026-05-21

### Added — Proactive deadline tracking

Commitments carry free-text deadlines ("Friday", "EOD Wednesday",
"2026-05-23") but until now nothing watched them. v0.11.0 adds the first
piece of the proactive layer: Verbatim now knows what's overdue and what's
due soon.

New `verbatim.deadlines` module resolves free-text deadlines to real dates
with the stdlib only — ISO dates, weekday names ("Friday" → next Friday),
"today"/"tomorrow", "next week", "in N days", and month-day forms
("May 23"). Filler like "EOD", "by", "before" is stripped first. It stays
conservative: genuinely ambiguous deadlines ("sometime soon") resolve to
`None` rather than guessing wrong.

`due_status` classifies a parsed deadline as overdue / due_today /
due_soon / scheduled / unknown.

**CLI**
- `verbatim query overdue` — open commitments past their deadline.
- `verbatim query due-soon [--within N]` — commitments due today or within
  N days (default 7).

**Web**
- New `/deadlines` page — overdue and due-soon commitments in two sections,
  with day-delta badges. "Deadlines" now sits in the sidebar with a live
  overdue count.

**Slack**
- `verbatim slack-bot nudge <channel> [--within N]` posts a deadline nudge
  — overdue + due-soon commitments — into a channel. Built for cron: a
  daily or Monday-morning nudge keeps slipping commitments visible. Web API
  only; the bot does not need to be running.

### Tests
- 25 new tests in `tests/test_deadlines.py`: deadline parsing across every
  supported form (ISO, weekday, relative, month-day, filler-stripping,
  next-year roll), `due_status` / `days_until` classification, the
  `state.deadlined / overdue / due_soon_commitments` helpers, and the
  `/deadlines` web route.

### Why
This is the start of the proactive layer. A memory tool that only answers
when asked is a filing cabinet; one that tells you "Qat's CULA commitment
is 3 days overdue" is a teammate. Deadline tracking is the highest-signal
proactive feature because the data is already there — every commitment has
a deadline — it just needed a date parser and somewhere to surface it.

## [0.10.2] — 2026-05-20

### Added — Calendar ingest (Google Calendar + Outlook)

`verbatim ingest-calendar google` and `verbatim ingest-calendar outlook`
pull meeting events and ingest each one as an extraction session. Most
teams put the agenda — and often the decisions and action items — straight
into the event description; that text is a real extraction source.

Each event becomes a unit carrying its title, organizer, attendees,
location, and full description / body. Recurring events are expanded into
individual instances (`singleEvents=true` for Google, `calendarView` for
Graph). Cancelled events are skipped.

- **Google Calendar** — Calendar API v3, token needs the
  `calendar.readonly` scope. `--calendar-id` selects a non-primary
  calendar.
- **Outlook / Microsoft 365** — Microsoft Graph `me/calendarView`, token
  needs the `Calendars.Read` scope. Plain-text bodies are preferred over
  HTML; `bodyPreview` is the fallback.

Auth: pass an OAuth access token via `--token` or
`$GOOGLE_CALENDAR_TOKEN` / `$OUTLOOK_CALENDAR_TOKEN`. The connector does
not run the OAuth dance itself — get a token from the Google OAuth
playground or Graph Explorer for ad-hoc use, or wire a refresh-token flow
upstream for unattended runs.

Events with no description and ≤1 attendee are skipped by default — they
spend tokens for no signal. `--include-empty` overrides. `--dry-run`
prints the ingest plan + cost estimate. `--since` / `--until` bound the
date window; `--auto-reconcile` merges into existing canonicals.

**Scope:** this ingests the calendar *event*, not the meeting *recording
transcript* (those live in Drive / SharePoint behind separate APIs).
Pairing an event with its recording is a later milestone.

### Tests
- 22 new tests in `tests/test_calendar.py` covering the `CalendarEvent`
  unit shape, `has_content` filtering, both clients' event parsing,
  pagination (Google `pageToken`, Graph `@odata.nextLink`), cancelled-event
  skipping, all-day events, plain-text-vs-HTML body selection, and the
  shared ISO / RFC-3339 helpers. HTTP is mocked via `httpx.MockTransport`.

### Why
Calendar is where meetings *start*. Ingesting the agenda closes the loop
before the meeting even happens — a decision written into an event
description is captured the same as one spoken aloud. It's also the
lowest-friction onboarding path: a new user with a calendar has ingestable
data on day one, no transcript export required.

## [0.10.1] — 2026-05-20

### Added — Slack HITL Edit modal + Reassign picker

The Edit and Reassign buttons on extraction cards (stubbed since v0.9.5)
are now live.

**Edit** opens a Slack modal (`views.open`) with kind-specific inputs —
commitment → deliverable / actor / deadline, decision → topic / outcome,
question → question / raised_by, blocker → blocked_thing / blocked_by /
owner. Inputs are pre-filled with current values; empty fields are
ignored. A free-form note field is saved to the audit log. On submit,
`apply_edit` rewrites the entity payload and mirrors actor/owner/raised_by
into the denormalized `primary_actor` column.

**Reassign** opens a modal with a Slack `users_select` picker. The picked
user's display name (resolved via `users.info`) becomes the new
`primary_actor`; an optional override field lets the operator type a name
directly. The Slack user id is captured in the audit row for later
identity resolution.

Modal submissions arrive as `view_submission` events, handled by the new
`_handle_view_submission`; the result is DM'd back to the submitter.

### Added — audit trail

New `entity_audit` table records every confirm / dismiss / edit / reassign
action with actor, timestamp, before/after JSON snapshots, and an optional
note. Older DBs get the table via an additive migration — no re-create.

- `verbatim audit <id>` — CLI command showing an entity's full history.
- Web entity-detail pages render an **Activity** timeline under the quote
  evidence.
- Confirm / Dismiss (live since v0.9.5) now write audit rows too.

### Slack app scopes

The Edit/Reassign modals need two additional Bot Token scopes beyond what
v0.9.5 required: nothing new for `views.open` (it's not scope-gated), but
**`users:read`** is now needed so Reassign can resolve a picked user's
display name. Add it under OAuth & Permissions and reinstall the app.

### Tests
- 27 new tests across `tests/test_slack_modal.py` (modal builders, apply_edit
  / apply_reassign across all four kinds, audit-row writing, view-submission
  end-to-end, Slack-user resolution, input-extraction helpers) plus updated
  assertions in `tests/test_slack_hitl.py` for the edit/reassign buttons'
  new modal-opening behavior.

### Why
Confirm / Dismiss alone make the Slack card a yes/no gate. Edit + Reassign
make it a real triage surface — a wrong actor or a vague deliverable can be
fixed in place instead of forcing a context-switch to the CLI. The audit
trail is the safety net: every in-Slack mutation is now traceable to a
person and a time.

## [0.10.0] — 2026-05-20

### Added — Person view

`verbatim query person <name>` and `/person/<name>` in the web UI aggregate
everything tied to a person across all four entity kinds:

- **Commitments** they're on the hook for (`primary_actor = name`)
- **Decisions** they participated in (JSON1 lookup on `payload.participants`)
- **Questions** they raised (`primary_actor = name`, kind=open_question)
- **Blockers** they own (`primary_actor = name`, kind=blocker)

The single-pane answer to "what's Alice on the hook for?", which used to
require running four separate filtered queries.

`verbatim query people` + `/people` list every distinct person who appears
in the state graph, sorted by frequency. Case-only duplicates ("Qat" vs
"QAT") fold into one entry.

A "People" item now sits in the web UI sidebar between Blockers and
Sessions, with a live count of known people.

Match is case-insensitive substring — `query person qat` resolves "Qat",
"Qatadah", or "qatcod" alike.

### Added — Docker image (ghcr.io/qatcod/verbatim-ai)

Multi-stage `Dockerfile` (python:3.12-slim builder + runtime) installs
verbatim-ai from a locally built wheel, runs as the unprivileged `verbatim`
user, persists state under `/data` (override with `$VERBATIM_DB_PATH`),
exposes 8765 for the web UI. ~376MB final image. Multi-arch
(linux/amd64 + linux/arm64).

`.github/workflows/docker.yml` builds and pushes to GitHub Container
Registry on every tag push and merge to main; `:edge` tracks main,
`:latest` and `:vX.Y.Z` track tags. Auth via `GITHUB_TOKEN` (no extra
registry secret needed).

### Added — Homebrew formula

`packaging/homebrew/verbatim-ai.rb` ships a complete Homebrew formula with
a `virtualenv_install_with_resources` install path. Once placed in a
`homebrew-verbatim` tap repo, users install with:

```bash
brew tap qatcod/verbatim
brew install verbatim-ai
```

See `packaging/homebrew/TAP_SETUP.md` for the one-time tap setup and
instructions for regenerating dependency SHAs with `homebrew-pypi-poet` on
each release.

### Added — README badges

PyPI version, supported Python versions, license, CI status, and PyPI
monthly downloads, rendered as shields.io badges at the top of the
README.

### Tests
- 16 new tests in `tests/test_person.py`: store.fetch_person across all four
  kinds (case-insensitive, substring, unknown-name, resolved filter,
  participants JSON1 path), list_known_people (frequency sort, case folding),
  /people route (renders + empty state), /person/<name> route (renders +
  unknown name + HTML escaping), sidebar nav link.

### Why
Person view is the question every new visitor asks first ("what does Alice
owe?") and used to require chaining four separate `--actor` filters.
Docker + Homebrew open install paths for users who don't want to think
about Python virtualenvs. Badges signal "alive, tested, version current"
at first glance.

## [0.9.7] — 2026-05-20

### Renamed PyPI distribution to `verbatim-ai`

The original `verbatim` name on PyPI is owned by an unrelated project, so
`pip install verbatim` resolves to someone else's package. We now publish
as `verbatim-ai`. The CLI command, the import name, and `src/verbatim/`
are unchanged — only the distribution name on PyPI changed:

```bash
pip install verbatim-ai     # was: pip install verbatim
verbatim --help             # unchanged
import verbatim             # unchanged
```

v0.9.6 was tagged but the publish step 403'd against PyPI (token scope vs.
existing project name), so this is the first version that actually lands
on PyPI.

## [0.9.6] — 2026-05-19

### Added — PyPI release pipeline

`.github/workflows/release.yml` ships a three-job pipeline that fires on
any `v*` tag push:

1. **build** — sets up Python 3.12, runs `python -m build` to produce sdist
   and wheel, verifies the tag matches `pyproject.toml`'s version, and runs
   `twine check` on both artifacts.
2. **publish-pypi** — publishes via [PyPI Trusted Publishing][tp] (OIDC,
   `pypa/gh-action-pypi-publish@release/v1`). No API token stored in the
   repo; the `pypi` GitHub Environment grants permission.
3. **github-release** — extracts the matching CHANGELOG section, attaches
   the built distributions, and creates the GitHub Release.

[tp]: https://docs.pypi.org/trusted-publishers/

Cutting a release is now:

```bash
# bump version in pyproject.toml + src/verbatim/__init__.py
# add CHANGELOG entry
git tag v0.9.6
git push origin v0.9.6
```

The verification step exits the workflow if the tag and `pyproject.toml`
version diverge — protects against the classic "tagged but forgot to bump"
mistake.

### Added — landing page

`docs/index.html` is a single-file landing page styled to match the
Verbatim design language (Inter + JetBrains Mono, commitment-violet
accent, dark/light auto-theming). Includes hero, terminal preview, the
four entity-type cards, surface grid, and an install snippet.

`.github/workflows/pages.yml` deploys `docs/` to GitHub Pages on every
push to `main` that touches the docs folder. `.nojekyll` is set so the
static HTML is served as-is.

### Why
Two artifacts launch needs: a one-line install (`pip install verbatim`)
and a URL to share. Both go live together when the first tag is pushed
post-launch.

## [0.9.5] — 2026-05-19

### Added — Slack interactive HITL

The Slack bot now posts rich Block Kit "extraction cards" with four action
buttons — **Confirm**, **Dismiss**, **Edit**, **Reassign** — so teams can
triage commitments / decisions / questions / blockers without leaving Slack.
A click flows through Socket Mode → `_handle_interactive` → `dispatch_action`,
which mutates entity status in the SQLite store and replies via the
interaction's `response_url` (with an ephemeral fallback if `response_url`
isn't usable).

Confirm/Dismiss are live and persist (`status` → `confirmed` / `dismissed`).
Edit and Reassign return "not yet implemented" placeholders so the UI is
discoverable today and the persistence can ship next without changing the
button surface.

### Added — `verbatim slack-bot post-card <channel> <entity_id>`

A CLI command for posting a card by entity ID (prefix-resolved) into a
channel — useful for demos, retroactive triage, and the daemon-mode workflow
where extractions surface in Slack on a schedule.

### Card design

- "verbatim" wordmark + entity type + `VRB-<short8>` ID in the header.
- The verbatim quote rendered as a Slack quote block (`>`).
- Actor / deadline / confidence shown as a fields row.
- All user-supplied strings are HTML-escaped before being sent to Slack
  mrkdwn so `<script>` / `</section>` / etc. in transcripts can't break the
  card layout or smuggle markup.
- Once an entity is resolved (confirmed / dismissed / resolved), the action
  row is hidden — the card becomes a historical record, not an active prompt.

### Tests
- 17 new tests in `tests/test_slack_hitl.py`: card structure (header, four
  action IDs, quote inclusion, post-resolution button removal, HTML escape),
  dispatch verbs (confirm / dismiss / edit / reassign / unknown / missing
  entity), `_handle_interactive` end-to-end (response_url path + ephemeral
  fallback), `post_extraction_card` (blocks posted, missing entity raises).

### Why
Most teams will not adopt yet another web dashboard for triage. They will
adopt a Slack button. HITL-in-Slack closes the loop between extraction and
ownership without forcing a context switch — and the same `dispatch_action`
seam will host the eventual reassign flow (Slack user picker) and edit modal.

## [0.9.4] — 2026-05-19

### Added — local LLM via Ollama

Verbatim now supports any Ollama-served model via its OpenAI-compatible
endpoint. No API key, no data leaving the host, $0 in cost.

Use it three ways:
- Per-call: `verbatim ingest meeting.txt --model ollama:llama3.1:8b`
- Default model: `export VERBATIM_MODEL=ollama:qwen2.5:7b`
- Backend toggle: `export VERBATIM_LLM_BACKEND=ollama` (uses `$VERBATIM_MODEL` as-is)

Ollama host defaults to `http://localhost:11434`; override with `$OLLAMA_HOST`.

### Refactored — pluggable LLM backend

`extractor.py` now dispatches between an Anthropic backend (`_extract_anthropic`) and an Ollama backend (`_extract_ollama`) at call time. Both produce the same `(ExtractionResult, ExtractionDiagnostics)` shape so every downstream caller — CLI, daemon, MCP server, web UI — is backend-agnostic. The `extract()` public function is unchanged; old callers keep working.

### Tool-use compatibility

Ollama models must support tool calling for the extractor to work. Known-good
as of 2026-05: `llama3.1:8b`, `llama3.1:70b`, `qwen2.5:7b`, `qwen2.5:14b`,
`mistral-small3.1:24b`. When a model returns text instead of a tool call,
the extractor raises with a clear hint pointing at known-good alternatives.

### Cost
Ollama models cost $0/M tokens. `cost.estimate_cost("ollama:...", ...)` returns 0.0 by virtue of being absent from `DEFAULT_PRICING` — the `verbatim query cost` table will just not include local models in its rollup.

### Tests
- 11 new tests in `tests/test_ollama_backend.py`: backend selection logic (model prefix + env var), happy-path tool call with correct token accounting, dispatch routing, error paths (no tool call, malformed JSON, HTTP failure), dict-shaped arguments tolerance, zero-cost confirmation.

### Why
Local LLM doubles the addressable user base — privacy-sensitive teams that
can't ship transcripts to Anthropic, and cost-sensitive teams who'd rather
run llama3.1 on a GPU they already own. Hot path for HN comments along the
lines of "I love it but I can't use it because of \[compliance / cost / air-gap\]."

## [0.9.3] — 2026-05-19

### Added — `verbatim watch` daemon mode

Long-running ingest daemons. Set it once, forget it.

- `verbatim watch slack-api [--channel X] [--interval 300]` polls Slack on an interval, ingests any new threads, auto-reconciles to dedupe. Min interval 30s to respect Slack rate limits. Auto-reconcile defaults ON in watch mode.
- `verbatim watch github owner/repo [--state all] [--interval 900]` polls GitHub PRs.
- Each iteration's spend is capped via `--max-cost-usd`. The loop continues to the next tick even when an iteration fails — single network blips don't kill long-running daemons.
- `--iterations N` runs N polls and stops; default is infinite. Useful in CI and tests.
- A small `--overlap` (default 60s for Slack, 120s for GitHub) handles clock drift and late-arriving messages — anything we pick up twice is collapsed by reconciliation.

Both commands honor Ctrl-C cleanly and print a per-tick summary line.

### Tests
- 6 new tests covering the generic loop (correct iteration count, resilience to failed iterations) and CLI validation (missing tokens, bad repo format).

### Why
Public users won't remember to run `ingest-slack-api` manually every five minutes. A daemon command turns Verbatim from "thing I run when I remember" into "background service that just works." For a freebie launching in June, that's the difference between abandonment after week one and sticky daily use.

## [0.9.2] — 2026-05-19

### Added — GitHub Issues + Jira projections

Two more projection targets, mirroring the v0.4 Linear projection's interface.

**GitHub Issues** (`verbatim project github`):
- `projections/github_issues.py` — REST API client (POST `/repos/{owner}/{repo}/issues`, PATCH for close), `GitHubIssuesClient` / `plan_projection` / `execute_projection` / `deactivate_projection` matching the Linear shape.
- Auth via `$GITHUB_TOKEN` (same token used by `ingest-github`).
- Maps `commitment.actor` → `issue.assignees[0]` (best-effort GitHub login), labels include `verbatim` plus any from `--label`.
- `verbatim project github --repo owner/name [--label X] [--min-confidence c] [--dry-run]`.

**Jira** (`verbatim project jira`):
- `projections/jira.py` — Atlassian Cloud REST v3 client with Basic auth (email + API token), minimal Atlassian Document Format builder for descriptions, transition-based close (looks up "Done" / "Closed" / "Resolved" by name).
- Auth via `$JIRA_EMAIL` + `$JIRA_API_TOKEN`.
- `verbatim project jira --site https://yourco.atlassian.net --project ENG [--issuetype Task] [--label X] [--dry-run]`.

Both share the same `projections` table — `verbatim project status --target github_issue` / `--target jira_issue` lists what's where. `verbatim unproject <id> --close-external` works for all three target kinds (Linear, GitHub, Jira).

### Tests
- 13 new tests in `tests/test_github_jira_projections.py`: constructor validation, idempotency, non-canonical skip, dry-run plan vs execute split, deactivate-with-close-external (PATCH for GitHub, transition lookup for Jira), ADF document structure.

### Why
Linear is the niche pick; GitHub Issues is universal and Jira is the corporate default. Adding both before the June public launch triples the addressable users without changing the data model — the projections table already supports arbitrary `target_kind`.

## [0.9.1] — 2026-05-19

### Added — cost guardrails

LLM spend is now visible and capped.

- New `cost.py` module: `estimate_cost(model, in_tokens, out_tokens)` mapping each Claude model to its public Anthropic pricing. Self-hosted users can override per-model rates via the `VERBATIM_PRICING` env var (`claude-sonnet-4-6:2.5/12,claude-opus-4-7:12/60`).
- New `--max-cost-usd` flag on every batch-ingest command (`ingest-slack`, `ingest-slack-api`, `ingest-github`). When set, the run aborts once cumulative cost crosses the cap and reports how many units were skipped.
- Running cost shown in each ingest's summary panel (`cost: $0.0432`).
- New `verbatim query cost` command — table of spend by model with totals + token counts.
- `verbatim query stats` now includes total spend and token counts.

### Why
Public users — especially the privacy/cost-skeptical engineering audience we'll get from Hacker News — are afraid of opaque LLM bills. Visibility + the ability to cap spend are the two biggest trust-builders before launch.

## [0.9.0] — 2026-05-19

### Added — `verbatim init` first-run wizard

A first-run experience designed to take a new user from `pip install verbatim` to seeing real extracted output in under 60 seconds.

```
$ verbatim init
Welcome to Verbatim. The AI memory layer for engineering teams.
✓ Created state DB at ~/.verbatim/state.db
✓ ANTHROPIC_API_KEY is set in your environment
Run a sample extraction now? (~$0.07, ~30s) [Y/n]: y
✓ Extracted 5 items: 2 commitments, 2 decisions, 1 questions, 0 blockers

Next steps:
  · Browse what's in the state graph: verbatim query commitments
  · Open the web UI:                   verbatim serve
  · Ingest your own transcript:        verbatim ingest path/to/meeting.txt
  · Wire up the Slack bot:             verbatim slack-bot run
```

Flags:
- `--yes` / `-y`: accept all defaults (CI-friendly, idempotent re-runs)
- `--skip-sample`: don't run the sample extraction step

The wizard never writes API keys to disk — it only verifies that `ANTHROPIC_API_KEY` is set in the environment and points users at the right URL to get one if it isn't. Re-running is safe: the DB-creation step is idempotent.

### Why
Before v0.9, the first-run path was "read the README, pick from 11 CLI commands, hope you guessed right." For a freebie launching in June where strangers from HN will arrive with no context, the first 60 seconds dominate the conversion rate. The wizard collapses that path into one command.

## [0.8.1] — 2026-05-19

### Added — OSS hygiene before public launch

- `CODE_OF_CONDUCT.md` — short, custom code of conduct (inspired by Contributor Covenant). Sets expectations for issue / PR / discussion conduct and the enforcement path.
- `SECURITY.md` — responsible-disclosure policy. Contact channel, expected response timeline, scope (in / out), and the trust model (single-user self-host default).

### Why
Going public in June. These two files are the standard signals that a repo is a real project, not a weekend hack — they sit at the top of GitHub's "Community" tab and get checked by potential users before they decide whether to try.

## [0.8.0] — 2026-05-19

Implementation of the Claude Design handoff. The web UI moves from a one-column dashboard to a Linear-style three-pane inbox shell. The original prototype is preserved at `docs/design/verbatim.html` for reference.

### Added — Three-pane inbox shell

`/` is now the Inbox: `[ sidebar 232px ] [ list pane 480px ] [ detail pane 1fr ]`. Selection happens via query param (`/?id=<entity_id>`), so URLs are linkable and the back button just works.

- **Sidebar**: brand mark + lowercase "verbatim" wordmark + workspace badge + chevron, persistent search box with `/` shortcut and ⌘K-style keycap, grouped nav (Workspace · Activity), animated ingestion pulse in the footer, theme toggle.
- **List pane**: title + count + filter tabs (All · Commitments · Decisions · Questions · Blockers, each with its colored type-dot and count), toolbar with status chip, scrollable list of rows.
- **List rows**: type-dot + `VRB-<short8>` mono ID, summary, owner avatar (deterministic-colored initials), due date with overdue/soon states, **inline italic quote underneath with attribution**, footer with source kind + channel + relative timestamp + confidence pill.
- **Detail pane**: breadcrumb, eyebrow with type dot, large entity title, **violet quote-hero card** with "verbatim" lock tag (the page-defining element — every other piece is metadata), additional sources block if the entity has merged siblings, right rail with Properties + Evidence (confidence bar + source).

### Added — Brand + typography

- Inter (weights 400/450/500/550/600/700) + JetBrains Mono via Google Fonts, with stylistic sets `cv11 ss01 ss03 cv02` enabled for cleaner letterforms.
- Per-kind colors: commitment violet `#a78bfa`, decision teal `#5eead4`, question amber `#fbbf24`, blocker rose `#fb7185`.
- Display ID format is now `VRB-<short8>` across every surface that shows an ID in the UI (rows, breadcrumbs, side rows).
- Deterministic avatar colors hashed from the actor name across {purple, teal, rose, amber, blue, slate} with dark + light palettes.

### Added — Light theme + persistence

Full light theme via `[data-theme="light"]` with `--accent: #7c3aed` (AA-balanced violet) and remapped per-kind colors. Theme persists in `localStorage` with a 4-line pre-paint script (no FOUC). Toggle button lives in the sidebar footer.

### Added — Legacy view preserved

The pre-v0.8 dashboard (stats + activity feed) moves to `/dashboard` and continues to work. Search at `/search` and entity-detail at `/entity/{id}` work as standalone pages, both rendered in the new design system.

### Where the design doesn't apply

The handoff includes a Slack thread reconstruction with a bot-reply mock under the quote hero. We don't actually have Slack message context in our DB (we have `source_label` + the single verbatim quote, not the surrounding messages), so that section is omitted rather than faked. It will return as a real feature when we ingest enough surrounding context to render it honestly.

### Tests
- 247 total (+19 new in `tests/test_design.py`). New tests pin the three-pane shell, quote-hero presence, type-dot colors per kind, VRB-short-id format, theme toggle + persistence, font loading, brand wordmark, breadcrumb, right-rail blocks, viewport meta. Existing 16 web tests + consistency tests updated for the new structure.

### Docs
- `docs/design/` now contains the original Claude Design handoff: `verbatim.html`, `bundle.jsx`, `README.md`, and `chat-original.md`. Future contributors should consult these before changing the visual system.

## [0.7.1] — 2026-05-19

A polish pass — no single inconsistency across surfaces.

### Fixed — cross-surface consistency
- **Entity IDs are now truncated to 8 chars + `…` everywhere.** Slack bot list views were using `[:10]`; brought into line with the web UI and CLI which already used `[:8]`.
- **"merged with N other source(s)" copy is now identical** across web entity detail, Slack `show`, and CLI `show`. Previously: web said "merged from N+1 sources", Slack said the same, CLI said "merged siblings: N". All three now read the same.
- **Pluralization is correct.** `1 other source` (singular) / `2 other sources` (plural). Tested explicitly.
- **Slack list view** now uses the compact `+N merged` pill form mirroring the web UI's pill, instead of the long parenthetical.

### Added — activity feed on dashboard
The plain "Recent sessions" table on the home page is replaced with a proper chronological activity feed (timestamp + event description + dot indicator). Future event kinds (reconciliation, projection-created) can plug into the same component without re-rendering.

### Added — accessibility
- **Skip-to-content link** at top of body, hidden until focused. Keyboard users can jump straight to `<main>` without tabbing through the entire sidebar.
- **`aria-current="page"`** on the active sidebar nav link.
- **`aria-label="Search Verbatim state"`** on the sidebar search input.
- **`aria-label="Primary"`** on the sidebar nav element.
- **Visible focus rings** via `:focus-visible` on every interactive element (links, buttons, inputs, selects). Uses the violet accent so the focus indicator matches the brand.
- **Decorative SVG icons** have `aria-hidden="true"` so screen readers skip them.

### Refined — wordmark
Slight typography tightening on the brand wordmark in the sidebar: tighter letter-spacing (-0.018em), font-weight 600, Inter's `ss01` + `cv11` stylistic alternates enabled for cleaner letterforms. The brand mark is unchanged (the v0.7.0 quote-pair design is the canonical mark until a new logo arrives).

### Tests
- 228 total (+15 new). All consistency contracts pinned by `tests/test_consistency.py`: ID truncation length, merged-source language match across web/Slack, pluralization, activity feed presence + empty-state, every a11y attribute.

## [0.7.0] — 2026-05-19

### Added — Cross-entity search
- New `state.search(query)` / `store.search_entities(query)` — case-insensitive substring match across `primary_actor`, `primary_topic`, `payload_json`, and `verbatim_quote`. Grouped results by kind plus a `source_match` bucket for entities matched only via a quote. Direct-match entities dedupe against source-match.
- New `GET /search?q=…` route. Group headers per kind, result rows with confidence badge + entity ref, matched quote inline with `<mark>` highlighting.
- Sidebar search box on every page (sticky). Carries the query back on the results page.
- Keyboard shortcut: pressing `/` anywhere focuses the sidebar search input (skipped when typing in any other input/textarea). 8 lines of inline JS, no build step.

### Added — Brand identity
- New Verbatim logomark: two pairs of pill-shaped marks tilted -12°, reading as an opening quote (lower-left) and a closing quote (upper-right) framing a captured statement — the negative space *is* the verbatim content. Pure inline SVG, ~280 bytes, scales to any size, follows `currentColor`.
- Replaces the v0.6.x placeholder mark (generic list-with-dot).
- Brief design rationale: the product's name *is* its core promise — every extraction must carry a verbatim quote. The mark visualizes that contract: quote → captured statement → quote.

### Tests
- 213 total (+21 new). Coverage: state.search behavior across all 4 kinds + source-only match + dedup + case-insensitivity, `/search` route empty / no-results / matched / highlighted, search box presence + query preservation across pages, keyboard-shortcut JS embedded, highlight() correctness + XSS escape.

### Why
- v0.6 had data and surfaces but no fast way to find a specific item. Search was the obvious gap. It's also the connective tissue between surfaces — once you've found something in the web UI, you can copy the entity-id prefix into the Slack bot's `/verbatim show` or the CLI's `verbatim show`.
- The placeholder logo from v0.6.0 was holding the brand back. v0.7.0's mark is the one that's worth putting in a deck.

## [0.6.2] — 2026-05-19

### Changed — web UI redesign

The v0.6 web UI was functional but plain. This release rewrites the visual system from scratch — same routes, same data model, same handler logic, just dramatically better-looking.

- **Sidebar navigation** replaces top nav. Grouped sections (*State* / *Activity*), inline SVG icons, brand mark with version badge.
- **Dark theme by default** with sophisticated token-based color palette (zinc + violet accent). `color-scheme: dark` set so form controls / scrollbars match.
- **Modern typography** — Inter font stack, tighter line-height, proper hierarchy (page header, section labels, body, mono).
- **Stat cards with icons** — each card has a colored icon glyph, hover state, larger numbers, better label/value contrast.
- **Cards wrap every table** — soft borders, subtle hover state on rows, gradient card headers, count chips.
- **Entity detail** redesigned as an article-style layout: kind/confidence/status as inline pills at top, a definition-list inside a subtle inner card, source quotes with violet-accent left border and metadata chips for timestamps.
- **Empty states** now have a title + hint + a copy-paste-ready command code block. So a first-time user sees `verbatim ingest path/to/meeting.txt` right in the empty Sessions page.
- **Responsive** — sidebar collapses to a horizontal nav on mobile.
- **Same XSS guarantees** — `html.escape` on every user-supplied value. The XSS test still passes unchanged.

Inline SVG icons (~10 KB total) — no external icon font, no CDN call, fully offline-capable. Whole page renders in ~18 KB.

### Tests
- 192 total, all still green. `test_active_nav_class_on_current_page` updated to match the new nav structure (anchor now contains an SVG before the label).

## [0.6.1] — 2026-05-19

### Fixed
- `ingest-slack-api` no longer aborts the whole run on the first channel the bot can't read. New `ChannelNotAccessible` exception type carries the channel name + the specific Slack error code + an actionable hint (invite the bot, or switch to a User token, or add a scope). The CLI now collects per-channel failures, prints a yellow `skipping #channel: <hint>` line, and continues with the rest. Only aborts when *every* requested channel was inaccessible.

### Why
Hit while dogfooding: running `verbatim ingest-slack-api --channel all-zakrs-tech` died on the first `conversations.history` call because the bot wasn't a member of that channel. There's no API-level workaround for that — `conversations.history` genuinely requires channel membership for a Bot token — but the error wasn't surfacing the fix. Now it does, and one bad channel doesn't take down the run.

Two fixes for the user when this happens:
1. **Invite the bot:** `/invite @YourBotName` inside the channel.
2. **Switch to a User token** (`xoxp-...` instead of `xoxb-...`): User tokens act as the human and see every channel that human can see, no invites needed. Add User Token Scopes (`channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`), reinstall the App, copy the User OAuth Token, set `SLACK_TOKEN=xoxp-...`.

### Tests
- 192 total (+3 since v0.6.0). New tests cover the `not_in_channel` → `ChannelNotAccessible` conversion path, the per-channel `on_channel_error` callback in `iter_units`, and the fail-fast preservation when no callback is provided.

## [0.6.0] — 2026-05-19

Two new consumer surfaces (read-mostly web UI, email digest) plus a hotfix for the v0.5 Slack bot.

### Added — `verbatim serve` (read-mostly web UI)
- New `src/verbatim/web.py` — Starlette app with dashboard, list pages per kind (commitments/decisions/open-questions/blockers), entity detail with all merged source quotes, sessions, projections.
- `verbatim serve [--host 127.0.0.1] [--port 8765] [--db ...]` boots `uvicorn` on the app.
- Hand-rolled HTML via f-strings + `html.escape` on every user-supplied value; tiny inline CSS, no build step, no Jinja files to package.
- Read-only for v0.6 by design — mutating HTTP endpoints without auth would be a footgun on a multi-user host. Resolve / link / unlink stay on the CLI for now.
- Filters on each list page: `?actor=`, `?owner=`, `?raised_by=`, `?min_confidence=`, `?ungrouped=1`. Inline `<form>` for browser users.
- 16 tests: every route renders, filters reflect in output, 404 on missing entity, **explicit XSS escape test** with `<script>`/`<img onerror>` in payloads.

### Added — `verbatim digest email`
- New `src/verbatim/email_digest.py` — renders the same digest shape used by Slack (stats + recent commitments + blockers + open questions) as both plain text *and* HTML; ships as multipart MIME via SMTP.
- `verbatim digest email --to addr [--to addr2] [--smtp-host X] [--smtp-port 587]` with config readable from `$SMTP_HOST` / `$SMTP_PORT` / `$SMTP_USER` / `$SMTP_PASSWORD` / `$SMTP_FROM`.
- STARTTLS by default, `--ssl` for SMTPS. SMTP-only on purpose — universal across providers (Gmail, SES SMTP interface, SendGrid SMTP, Postmark SMTP).
- 13 tests: render returns text + HTML, content gates on what's actually in the DB, HTML escapes user content, MIME structure correct, SMTP send mocked across STARTTLS / SSL / no-auth paths.

### Fixed — Slack bot `not_in_channel`
The bot was using `chat.postEphemeral` to reply to slash commands, which Slack rejects with `not_in_channel` whenever someone runs `/verbatim` in a channel the bot hasn't been invited to. v0.6 switches to Slack's per-invocation `response_url` (POST JSON, no channel membership required) with a three-stage fallback: `response_url` → `chat.postEphemeral` → open a DM with the user. Means `/verbatim` works in any channel the user is in, regardless of where the bot has been invited.

### Dependencies
- `starlette >= 0.36.0` (already transitive via `mcp`, now explicit)
- `uvicorn[standard] >= 0.27.0` (already transitive, now explicit)

### Tests
- 189 total, all green. +29 new (16 web, 13 email) + reworked slash-command fallback tests.

## [0.5.0] — 2026-05-19

The consumer-facing surface. Non-technical users no longer need a terminal — they interact with Verbatim from inside Slack.

### Added — Slack bot (Socket Mode)

- New `slack_bot.py` module — `VerbatimSlackBot` class with Socket Mode connection, slash-command dispatch, ephemeral replies, and digest posting.
- **No public URL required.** Uses Slack's Socket Mode (outbound WebSocket from the bot to Slack), so the bot deploys on a laptop, a self-hosted VM, anywhere with outbound internet. No ngrok, no Cloudflare tunnel, no nginx.
- Slash command `/verbatim` supports:
  - `commitments [actor]` — open commitments, optionally filtered
  - `decisions` — recent decisions
  - `questions` / `open-questions` / `open` — unresolved questions
  - `blockers [owner]` — current blockers
  - `stats` — overall counts
  - `show <id-prefix>` — entity detail with all source quotes
  - `help` (also shown for empty input or unknown subcommand)
- Replies are **ephemeral** (only the invoker sees them) so channels stay quiet. Falls back to in-channel post when no user_id is present.
- `verbatim slack-bot run` — long-running bot process. Connects via Socket Mode, blocks until Ctrl-C.
- `verbatim slack-bot digest <channel>` — one-shot digest post. Useful from cron (e.g. weekly Monday morning) or after batch ingest. Doesn't need Socket Mode — just the bot token + Web API.
- 31 tests: command parsing, all formatters, dispatch over a seeded SQLite, constructor validation, slash-command handler routing (ephemeral vs in-channel vs no-op), digest content. `WebClient` mocked via injectable `FakeWebClient`. Socket Mode loop intentionally not exercised — it's a thin wrapper around Slack's WebSocket and would only test their SDK.

### Setup (one-time, in the same Slack App you use for `ingest-slack-api`)

1. **Settings → Socket Mode** → Enable. Generate App-Level Token with `connections:write` scope. Copy `xapp-...`.
2. **Features → Slash Commands** → Create New Command. Command: `/verbatim`. Request URL can be any placeholder (Socket Mode doesn't use it).
3. **OAuth & Permissions → Bot Token Scopes** → add `chat:write` and `commands` (you already have `channels:history`, `channels:read`, `users:read` from `ingest-slack-api`).
4. Reinstall the App. Run: `SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... verbatim slack-bot run`.

### Dependencies
- `websocket-client >= 1.6.0` (Socket Mode transport)

### Why this is the unlock
v0.4.x was a complete operator tool — engineers and admins could ingest, reconcile, project. But the people whose meetings produce the state (PMs, leads, execs) had no surface. v0.5 fixes that. The CLI stays the operator tool; the bot is everyone else.

The intentional next surface after this is `verbatim serve` — a read-mostly web view for browsing state and walking through merged entities. v0.6 territory.

## [0.4.1] — 2026-05-19

### Added
- `--auto-reconcile` and `--reconcile-threshold` flags on all four ingest commands (`ingest`, `ingest-slack`, `ingest-slack-api`, `ingest-github`). When enabled, every newly extracted entity is checked against existing canonicals of the same kind and merged in if similarity ≥ threshold. Ingest summaries now report the per-run reconciliation count when the flag is on.

### Why
v0.4.0 added reconciliation as a separate post-hoc step. With continuous ingest paths (live Slack + GitHub), running `reconcile` periodically is fine, but having merging happen at the point of new ingestion keeps the queryable state coherent in real time and avoids a "between-runs" window where duplicates exist.

## [0.4.0] — 2026-05-19

The two pieces that move Verbatim from "extractor" to "memory layer": entities now reconcile across sources, and the reconciled state pushes outward to Linear.

### Added — Cross-session reconciliation
- New `canonical_id` + `merged_at` columns on entities. A canonical entity has `canonical_id IS NULL`; merged siblings point at their canonical's id. Idempotent migration covers DBs created on earlier versions.
- New `reconcile.py` module — rapidfuzz token-set similarity over `primary_topic`, scoped by `kind` + `primary_actor`. Default threshold 88 (deliberately conservative — false-positive merges silently rewrite state).
- `verbatim reconcile` CLI command — sweep mode with `--threshold`, `--kind`, `--dry-run`.
- `verbatim link <canonical> <member>` and `verbatim unlink <id>` for manual corrections. Linking through an existing chain resolves to the root canonical (no orphaned mid-chain pointers); linking a canonical with children re-roots the children too.
- `verbatim show <id>` — detail view with folded sources from all merged siblings.
- `auto_reconcile=True` option on `state.save_extraction` (and via `verbatim ingest`/etc. callers when wired) so new ingests can auto-merge against prior state.
- All query commands now default to canonical-only view (merged siblings folded into their canonical with `merged_count` and combined `sources`). `--ungrouped` shows the raw flat list.

### Added — Linear projection
- New `projections/` subpackage. First inhabitant: `projections/linear.py`.
- `LinearClient` — minimal GraphQL client over httpx. `viewer`, `list_teams`, `list_users`, `list_workflow_states`, `create_issue`, `archive_issue`.
- `build_user_resolver` — best-effort actor → Linear user-id by displayName/name/email-local.
- `render_issue_from_commitment` — pure function that builds an `IssueDraft` (title, description, assignee_id, due_date) from a Verbatim commitment entity. Embeds source quotes + Verbatim entity id in the issue description for traceability. Deadline only sets `dueDate` if it parses as a real date — natural-language deadlines like "EOD Friday" deliberately *don't* become Linear due-dates.
- `plan_projection` / `execute_projection` — idempotent split: planning is pure (decides skip vs project), execution does the API call + persists.
- New `projections` table — tracks `(entity_id, target_kind)` with unique constraint on `(entity_id, target_kind, status='active')`, so re-running `verbatim project linear` doesn't duplicate.
- `verbatim project linear --team <name|key> --state <name> [--api-key X] [--min-confidence high] [--dry-run]` — push pending commitments. `verbatim project status` lists active projections. `verbatim unproject <id> [--close-external]` deactivates our tracking; with `--close-external` also archives the Linear issue.

### Tests
- 22 reconciliation tests (similarity scoring, candidate search, link/unlink semantics including chain resolution and child re-rooting, folded vs ungrouped views, auto-reconcile on ingest, stats).
- 20 Linear tests (mocked GraphQL via httpx.MockTransport, user-resolver fallback ladder, issue rendering, projection planning idempotency, archive-on-unproject).
- Total suite: 127 tests, all green. No network in any test.

### Dependencies
- `rapidfuzz >= 3.5.0` (deterministic topic similarity)

### Design notes — what's deliberately *not* in here
- LLM-assisted reconciliation: this version is rapidfuzz-only because false-positive merges are hard to recover from in a memory-layer product. Adding an LLM-judge pass is a follow-on once we have real corpus signal on what the threshold misses.
- Projection of non-commitment kinds to Linear. The schema and CLI are kind-agnostic but the rendering today only knows commitments. Decisions/questions/blockers can be added without schema changes.
- Linear → Verbatim sync-back (issue status changes flowing into entity resolution). One-way push only for v0.4.

## [0.3.0] — 2026-05-18

### Added
- **Slack Web API connector** (`slack_api.py`) — pull live messages from a Slack workspace via OAuth token. Same `SlackUnit` output as the export connector, so it flows through the existing pipeline unchanged. Uses `slack_sdk.WebClient`; handles pagination, thread expansion via `conversations.replies`, in-process user cache.
- **`verbatim ingest-slack-api`** CLI command with `--token` (or `$SLACK_TOKEN` env), `--channel`, `--since/--until`, `--min-thread-messages`, `--include-loose`, `--include-private`, `--limit`, `--dry-run`, `--request-pause` for rate-limit-aware pacing.
- **GitHub PR connector** (`github_pr.py`) — pull a repo's pull request discussion threads (PR body + issue comments + review comments) as extraction units. Uses raw `httpx` against the GitHub REST API; no `gh` CLI shell-out, no PyGithub dep.
- **`verbatim ingest-github`** CLI command with `--token` (or `$GITHUB_TOKEN` env), `--pr` (specific PRs), `--state` (open/closed/all), `--since/--until` (date window), `--limit`, `--dry-run`.
- Shared Slack ingestion primitives extracted to `connectors/slack_common.py` — both export and API connectors now build SlackUnits via one builder function. Existing `slack_export` API preserved via re-exports; no test changes required for the refactor.
- New `_run_unit_ingest` CLI helper — generic ingest loop usable for any unit type with `.transcript`/`.source_label`/`.source_kind`. GitHub PR units use it directly.
- 20 new tests (11 slack_api with mocked WebClient, 9 github_pr with mocked httpx); suite now 85 tests, all green.

### Dependencies
- `slack-sdk >= 3.27.0` (live Slack Web API)
- `httpx >= 0.27.0` (GitHub REST + test transport)

### Design notes
- Slack Web API connector and Slack export connector now share the same SlackUnit / thread-building / transcript-rendering code. The two differ only in *where the raw messages come from*.
- GitHub PR units have a parallel `.transcript`/`.source_label`/`.source_kind` shape so the same ingest pipeline works for any future connector that conforms.
- v1 wedge: token-based auth, no hosted OAuth flow. Self-hosted users create their own Slack App / GitHub PAT — they keep the token, we never see it. Hosted OAuth becomes a v1.x add when there's a hosted Verbatim to OAuth against.

## [0.2.0] — 2026-05-18

### Added
- **Slack export connector** — ingest a Slack workspace export ZIP (or extracted directory) directly. No OAuth, no Slack API setup; the export is a file the customer can already produce. First multi-source ingestion path beyond meeting transcripts.
- `verbatim ingest-slack <path>` CLI command with filtering flags:
  - `--channel` (repeatable) to restrict to specific channels
  - `--since` / `--until` for date windowing
  - `--min-thread-messages` (default 3) to skip noise threads
  - `--include-loose` for channel-day rollups of non-threaded messages
  - `--limit` and `--dry-run` for cost control + plan preview
- Threads become their own extraction sessions tagged `source_kind = "slack_thread"`, with `source_label` of the form `slack://#channel/thread/<timestamp>` for traceability
- User-ID mention resolution (`<@U12345>` → `@display-name`) using the export's `users.json`
- 23 Slack connector tests against a fixture export

### Design notes
- Thread-level granularity is the v1.0 default — a thread is the closest Slack analog to a meeting and yields cleaner state than dumping a channel of unrelated messages into one extraction.
- Noise subtypes (channel_join, pinned_item, bot_add, etc.) are filtered at parse time.
- Connector lives at `src/verbatim/connectors/slack_export.py` — first inhabitant of the connector framework that will hold GitHub, Anthropic Console, CloudTrail, etc. in future releases.

## [0.1.1] — 2026-05-18

### Added
- Unit test suite (42 tests) covering schema, transcripts, store, state, and renderers
- `CONTRIBUTING.md` with the two non-negotiables (verbatim quoting, calibrated confidence) and guides for adding transcript parsers, connectors, projections, and MCP tools
- GitHub Actions CI: lint (ruff) + pytest on Python 3.10–3.12 matrix

### Changed
- **Tightened the extraction prompt** based on observed over-extraction in v0.0:
  - `HIGH` confidence now requires substantive weight, not just explicitness — casual asides and offhand favors are `MEDIUM` at most
  - New rule: respect explicit speaker scope-flags ("that's separate from X") — skip or extract at `LOW` with a `notes` annotation
  - New rule: distinguish open questions (needs an answer) from stated concerns (contingency / "if X then Y" hedging) — concerns are not questions
  - Anti-fragmentation guidance: don't split a single deliverable into multiple commitments
- pytest config disables `pytest-asyncio` plugin to avoid collection conflicts from transitive `mcp` deps

### Why these changes
First real-API extraction on the included sample transcript flagged three calibration issues:
- A casual "just use my key, I'll cover it" was treated as a high-confidence commitment
- A statement the speaker explicitly scoped out ("that's separate from Verbatim") still ended up in the commitments list
- A contingency ("if quality is bad we may need to iterate") was extracted as an open question

The prompt changes target each issue specifically. Validate with `verbatim extract examples/sample_transcript.txt`; you should see fewer false positives without losing the real items.

## [0.1.0] — 2026-05-18

### Added (initial release)
- CLI commands: `extract` (file output), `ingest` (persist to DB), `query` (read state), `resolve` (mark resolved)
- Pydantic schema with `SourceReference` requirement on every extracted entity
- WebVTT + plain text + stdin transcript parsers
- Anthropic API extraction via forced tool use
- JSON + Markdown renderers
- SQLite-backed state graph (sessions, entities, entity_sources)
- MCP server (`verbatim-mcp`) exposing 7 tools to agent clients (Claude Code, Cursor, etc.)
- Self-hostable, BYO LLM key, MIT licensed
