# Verbatim

> The AI memory layer for engineering teams. Continuously synthesizes your meetings, Slack, and code collaboration into a single source of truth — and exposes that state to any AI agent as a tool.

**Status:** v0.2 — CLI extraction, SQLite-backed state graph, MCP server. Connectors for Slack / GitHub / Linear projection on the v1 roadmap.

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
| **v0** ✅ | CLI extraction. Confidence + source quoting. JSON + Markdown out. |
| **v0.2** ✅ (current) | SQLite state graph. `ingest` / `query` / `resolve` CLI. MCP server. |
| **v1** | Slack + GitHub PR comment connectors. Cross-session reconciliation. Linear projection. Web UI. |
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
