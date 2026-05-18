# Verbatim

> The AI memory layer for engineering teams. Continuously synthesizes your meetings, Slack, and code collaboration into a single source of truth — and exposes that state to any AI agent as a tool.

**Status:** v0.6.0 — CLI extraction, SQLite state graph with cross-session reconciliation, MCP server, live Slack + Slack export + GitHub PR threads, Linear projection, Slack bot with slash commands + digest, **web UI (`verbatim serve`)**, **email digest (`verbatim digest email`)**, calibrated prompt, 189-test pytest suite, CI.

---

## Why this exists

Most "AI notetakers" extract action items once per meeting and forget. They sit in your inbox, decoupled from the rest of your team's state, asking you to triage them. They don't reconcile, don't follow up, don't know what your team is actually working on.

Verbatim is different. Three insights:

1. **Continuous state, not point-in-time extraction.** Decisions, commitments, blockers, and open questions are *remembered*, reconciled across conversations, and updated as work happens.

2. **Multi-source, not meeting-only.** Most decisions happen *outside* meetings — in Slack threads, PR comments, DMs. A product that only ingests meetings misses the majority of team state. (v1+)

3. **Agent-native, not just human-facing.** Verbatim ships an MCP server, so Claude Code, Cursor, and any AI agent can query "what's blocking the Xero deal?" or "what did Qat commit to this week?" as a tool — with sourced, verbatim quotes.

Plus the non-negotiables: open source, self-hostable, bring your own LLM, every extraction sourced to a verbatim quote from the input.

## What works today

- **CLI extraction.** Take a meeting transcript, get structured commitments, decisions, open questions, and blockers — each with a confidence score and the verbatim quote that supports it.
- **Persistent state.** Extractions accumulate in a local SQLite store. Query it from the CLI or expose it to AI agents via MCP.
- **MCP server.** Wire `verbatim-mcp` into Claude Code or Cursor and the agent can read your team state as a tool.

## Install

```bash
git clone <repo-url> verbatim
cd verbatim
pip install -e .
```

## Quick start

### One-shot extraction (write files, no persistence)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
verbatim extract path/to/meeting.txt
# writes meeting.verbatim.json + meeting.verbatim.md alongside the input
```

Supports plain `.txt`, WebVTT `.vtt`, and stdin (`-`).

### Persist into the state store

```bash
verbatim ingest path/to/meeting.txt
# extraction goes into ~/.verbatim/state.db
```

Set `VERBATIM_DB_PATH` to override the database location.

### Ingest a Slack workspace export

```bash
# Dry-run first to see what would be extracted and estimate cost
verbatim ingest-slack ~/Downloads/your_workspace_slack_export.zip --dry-run

# Then ingest for real
verbatim ingest-slack ~/Downloads/your_workspace_slack_export.zip \
    --channel engineering --channel architecture \
    --since 2026-04-01 \
    --min-thread-messages 4
```

Each Slack thread (≥ `--min-thread-messages` messages) becomes its own extraction session. The output looks the same as ingesting a meeting transcript — same schema, same state graph — except `source_kind` is `slack_thread` and `source_path` is `slack://#channel/thread/<timestamp>`.

Useful flags:

| Flag | Use |
|---|---|
| `--channel` / `-c` | Restrict to specific channels (repeatable) |
| `--since` / `--until` | ISO date window (YYYY-MM-DD) |
| `--min-thread-messages` | Skip threads shorter than this (default 3) |
| `--include-loose` | Also extract channel-day rollups of non-threaded messages |
| `--limit` / `-n` | Stop after N units (cost cap) |
| `--dry-run` | Show plan + cost estimate without making API calls |

Get a Slack export from `Slack admin → Settings → Import/Export Data → Export`. No OAuth, no app, no scopes — the file is yours already.

### Ingest a *live* Slack workspace (Web API)

If you can't always export, use the live connector instead. One-time setup: create a Slack App and get a token.

```bash
export SLACK_TOKEN=xoxb-...   # or xoxp-...
verbatim ingest-slack-api --channel engineering --since 2026-05-01 --dry-run
verbatim ingest-slack-api --channel engineering --since 2026-05-01
```

**Slack App setup (one-time):**
1. https://api.slack.com/apps → *Create New App* → *From scratch*. Pick a name and your workspace.
2. *OAuth & Permissions* → add Bot Token scopes:
   - `channels:history` (read public channel messages)
   - `channels:read` (list public channels)
   - `users:read` (resolve user IDs to names)
   - Optionally: `groups:history`, `im:history`, `mpim:history` for private channels/DMs.
3. Install the App to your workspace. Copy the *Bot User OAuth Token* (`xoxb-...`).
4. `/invite @YourBotName` into any public channel you want it to read.

For a User token (sees everything you see, including private DMs you're in), use *User Token Scopes* instead with the same names. Install/Reinstall the App and copy the *User OAuth Token* (`xoxp-...`).

Flags are the same as `ingest-slack` plus `--include-private`, `--request-pause` (seconds between Slack API calls if you hit rate limits).

### Ingest GitHub PR discussions

Most engineering decisions and commitments live in PR review threads. The GitHub connector pulls a PR's body, issue comments, and review comments (with file/line context), and treats each PR as one extraction unit.

```bash
export GITHUB_TOKEN=ghp_...
# specific PR(s)
verbatim ingest-github qatcod/verbatim --pr 42 --pr 43
# all PRs updated since a date
verbatim ingest-github qatcod/verbatim --state all --since 2026-05-01 --dry-run
```

PAT needs `repo` scope (or `public_repo` for public-only). Fine-grained tokens need Read on "Pull requests" and "Issues" for the target repos.

### Query accumulated state

```bash
verbatim query stats
verbatim query commitments --actor Qat --min-confidence high
verbatim query decisions
verbatim query open-questions --raised-by Jason
verbatim query blockers
verbatim query sessions

# mark something as resolved
verbatim resolve <entity-id-prefix>
```

### Reconcile duplicates across sources

When the same commitment shows up in a meeting *and* a Slack thread, they should be one entity with multiple source quotes, not two separate items.

```bash
# Preview which entities would be merged
verbatim reconcile --dry-run

# Apply at default threshold (88 — strict)
verbatim reconcile

# Tune the threshold if you have a noisy corpus
verbatim reconcile --threshold 92
```

The matcher uses rapidfuzz token-set similarity scoped by kind + actor — same-actor, same-kind, topic-similarity ≥ threshold. False-positive merges silently rewrite state, so the default is deliberately strict; loosen only with `--dry-run` first.

Manual corrections:

```bash
verbatim link <canonical-id> <member-id>   # merge member into canonical
verbatim unlink <id>                       # restore to standalone canonical
verbatim show <id>                         # see the canonical entity + all merged sources
```

Query commands fold merged siblings into canonicals by default — pass `--ungrouped` to see the raw list. Counts in `verbatim query stats` are canonical-only.

Auto-reconcile during ingest (so live Slack messages reconcile against your meeting state in real time): pass `auto_reconcile=True` to `state.save_extraction` in code. CLI flag exposure for the various ingest commands is a follow-on.

### Push commitments to Linear

```bash
export LINEAR_API_KEY=lin_api_...

# Preview the plan
verbatim project linear --team Engineering --state Backlog --dry-run

# Apply
verbatim project linear --team Engineering --state Backlog

# See what's been projected
verbatim project status

# Deactivate one projection; optionally archive the Linear issue too
verbatim unproject <projection-id-prefix>
verbatim unproject <projection-id-prefix> --close-external
```

What gets pushed: commitments at the configured confidence (default `high`) that have not already been projected. Idempotent — running twice does nothing the second time. The Linear issue's description carries the verbatim source quote(s) and the Verbatim entity id, so you can always trace a Linear ticket back to its origin in a meeting or Slack thread.

Assignee resolution is best-effort: actor name → Linear user via displayName, then name, then email-local-part. No match → unassigned. The actor string is preserved in the description so it's never lost.

Deadline → Linear `dueDate` only when the deadline parses as a real date (`2026-05-23`, `2026-05-23T10:00:00Z`). Natural-language deadlines like "EOD Friday" deliberately don't become due-dates — you'd be promising precision the extractor didn't actually have.

### Run the Slack bot — let non-technical users query state from inside Slack

Engineers and admins use the CLI. Everyone else uses Slack. The bot lets your team run `/verbatim commitments`, `/verbatim show abc12345`, `/verbatim stats`, etc. directly from any channel, without ever opening a terminal.

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
verbatim slack-bot run                       # long-running, Ctrl-C to stop
verbatim slack-bot digest \#engineering      # one-shot summary post
```

**Slack App setup (one-time, on the same App you use for `ingest-slack-api`):**

1. **Settings → Socket Mode** → Enable. Generate an App-Level Token with `connections:write`. Copy the `xapp-...` token.
2. **Features → Slash Commands** → Create New Command. Command: `/verbatim`. Description: "Query your team's Verbatim state." Request URL: any placeholder — Socket Mode doesn't use it.
3. **OAuth & Permissions → Bot Token Scopes** → add `chat:write` and `commands` (alongside the `channels:history` / `channels:read` / `users:read` you already have for ingest).
4. Reinstall the App. The bot will work on the *existing* `xoxb-...` token plus the new `xapp-...`.

**No public URL required.** Socket Mode opens an outbound WebSocket from your bot process to Slack — your bot runs on a laptop, a self-hosted VM, anywhere with outbound internet. No ngrok, no Cloudflare tunnel, no nginx.

**Slash commands supported:**

| Command | What it does |
|---|---|
| `/verbatim commitments [actor]` | Open commitments, optionally filtered by name |
| `/verbatim decisions` | Recent decisions |
| `/verbatim questions` | Unresolved questions |
| `/verbatim blockers [owner]` | Current blockers, optionally filtered |
| `/verbatim stats` | Counts overview |
| `/verbatim show <id-prefix>` | Entity detail with all source quotes |
| `/verbatim help` | Show available commands |

Replies are ephemeral (only the invoker sees them) so channels stay quiet. Use `verbatim slack-bot digest <channel>` for explicit shared digests.

### Browse state in a browser — `verbatim serve`

Read-mostly web UI for non-technical browsing of the state graph.

```bash
verbatim serve                        # http://127.0.0.1:8765
verbatim serve --host 0.0.0.0 --port 8080
```

Routes: dashboard, commitments, decisions, open-questions, blockers, sessions, projections, and `/entity/<id>` with all merged source quotes. Each list page has inline filters (actor / owner / raised_by / min_confidence / show-merged-siblings-separately).

**Local-only by default.** The UI is read-only in v0.6 and binds to `127.0.0.1`. Multi-user with SSO / token auth is the v0.7 story; for now treat this as a local browser tool you point your colleagues at when they're on the same network or a tunneled session.

### Email a digest — `verbatim digest email`

For people who live in email more than Slack — execs, CFOs, anyone who wants a Monday-morning weekly summary.

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_USER=bot@example.com
export SMTP_PASSWORD=...
verbatim digest email --to qat@example.com --to jason@example.com
```

Renders the same content as the Slack `digest` (stats + recent commitments + open blockers + open questions) into multipart MIME — both plain text and HTML. STARTTLS by default; `--ssl` for SMTPS. SMTP works across every provider (Gmail SMTP, SES SMTP interface, SendGrid SMTP, Postmark SMTP, your own mailserver). Drop into cron for recurring digests.

### Wire MCP into Claude Code

Add to your Claude Code config (`~/.claude/settings.json` or workspace settings):

```json
{
  "mcpServers": {
    "verbatim": {
      "command": "verbatim-mcp"
    }
  }
}
```

Restart Claude Code. Then you can ask: *"What did the team decide about the database choice?"* or *"What's Qat committed to this week?"* and Claude will call into Verbatim's state to answer with sourced quotes.

The MCP server exposes seven tools:

| Tool | Use |
|---|---|
| `list_commitments` | Open commitments, filterable by actor / confidence |
| `list_decisions` | Past decisions, filterable by confidence |
| `list_open_questions` | Unresolved questions, filterable by who raised them |
| `list_blockers` | Current blockers, filterable by owner |
| `recent_sessions` | Recent extraction sessions for source-tracing |
| `verbatim_stats` | Quick counts of open items |
| `resolve_entity` | Mark a commitment/decision/etc. as resolved |

## Sample output

```markdown
# Meeting summary
Kickoff for the Verbatim project. Qat to ship v0 by Wednesday,
Taz to review Thursday morning, public release decision Thursday afternoon.

## Commitments
- **Qat** — working v0 of Verbatim CLI by **end of day Wednesday**  (high confidence)
  > [00:51] Qat: I'll have a working version by end of day Wednesday.

## Decisions
- **language choice for v0** → Python  (high confidence)
  - Participants: Qat, Taz
  - Rationale: Iteration speed; MCP server language can be decided later.
  - Alternatives considered: TypeScript
  > [01:25] Qat: Python for v0. The MCP server can be either, we'll decide when we get there.

## Open questions
- **API cost model** (raised by Taz → Jason) — What's the budget for ongoing API tokens?  (medium confidence)
  > [03:58] Taz: do we have a budget for the Anthropic API tokens?
```

## Roadmap

| Phase | What |
|---|---|
| **v0.1.0** ✅ | CLI extraction. Confidence + source quoting. JSON + Markdown out. SQLite state graph. `ingest` / `query` / `resolve` CLI. MCP server. |
| **v0.1.1** ✅ | Prompt calibration tweaks. 42-test pytest suite. GitHub Actions CI. Contribution guide. |
| **v0.2.0** ✅ | Slack workspace export connector. `ingest-slack` CLI with channel/date/length/limit/dry-run filters. |
| **v0.3.0** ✅ | Live Slack Web API connector (`ingest-slack-api`) + GitHub PR connector (`ingest-github`). Shared Slack primitives in `slack_common`. |
| **v0.4.0** ✅ | Cross-session reconciliation with `canonical_id`/`merged_at`, rapidfuzz matcher, `reconcile`/`link`/`unlink`/`show` commands, folded-by-default queries. Linear projection with idempotent issue creation, user resolution, verbatim-quote-bearing descriptions. |
| **v0.4.1** ✅ | `--auto-reconcile` flags on every ingest command so continuous-ingest paths merge in real time. |
| **v0.5.0** ✅ | Slack bot with Socket Mode (no public URL) — `/verbatim` slash commands for non-technical users, ephemeral replies, digest posting. |
| **v0.6.0** ✅ (current) | Read-mostly web UI (`verbatim serve`). Email digest (`verbatim digest email`) over SMTP. Slack bot `not_in_channel` fix — now uses `response_url` so `/verbatim` works in any channel. 29 new tests. |
| **v1** | LLM-assisted reconciliation. Linear → Verbatim sync-back. Auth + mutating web endpoints. Hosted-SaaS deployment path. |
| **v2** | Identity resolution across systems. Confidence-gated review queue. Slack bot for queries. |
| **v3** | Proactive agents — auto-standup, deadline nudges, contradiction detection, status-report generation. |

## Design principles

- **Source-linked or it didn't happen.** Every extracted item carries a verbatim quote. No hallucinated commitments — if the model can't quote it, it can't claim it.
- **Confidence-scored.** Medium and low items are surfaced but not auto-projected to trackers.
- **Self-hostable from day one.** Meeting transcripts and team chats are the most sensitive corpus a company owns. Verbatim runs in your environment, with your LLM key.
- **Bring your own model.** Anthropic by default; pluggable. Set `VERBATIM_MODEL` to override.
- **Don't replace your tracker — project into it.** Linear, GitHub Issues, Jira stay your source of truth for *work*. Verbatim is the source of truth for *team state*, and projects relevant slices into the tools you already use.
- **Agent-first.** The MCP interface is a peer to the human CLI, not an afterthought.

## Architecture

```
            ┌──────────────────────────┐
            │   transcripts / chats    │
            │   (v0: .txt/.vtt files;  │
            │    v1: Slack, GitHub…)   │
            └────────────┬─────────────┘
                         │
                         ▼
            ┌──────────────────────────┐
            │   extractor (Anthropic   │
            │   tool-use → Pydantic)   │
            └────────────┬─────────────┘
                         │
                         ▼
            ┌──────────────────────────┐
            │   SQLite state graph     │
            │   (sessions, entities,   │
            │    source quotes)        │
            └─────┬──────────────┬─────┘
                  │              │
       ┌──────────▼─┐       ┌────▼─────────────┐
       │   CLI      │       │  MCP server      │
       │ (humans)   │       │  (agent clients) │
       └────────────┘       └──────────────────┘
                                     │
                                ┌────▼─────────┐
                                │ Claude Code, │
                                │ Cursor, …    │
                                └──────────────┘
```

## Contributing

Connectors and projection targets are intentionally modular. If you want to ship a connector for your team's tool of choice (Slack history, GitHub audit, Linear push), see `CONTRIBUTING.md` (coming with v1).

## License

MIT. Built by Qat.
