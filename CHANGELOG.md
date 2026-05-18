# Changelog

All notable changes to Verbatim. This project follows [Semantic Versioning](https://semver.org/).

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
