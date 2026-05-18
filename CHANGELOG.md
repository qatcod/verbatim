# Changelog

All notable changes to Verbatim. This project follows [Semantic Versioning](https://semver.org/).

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
