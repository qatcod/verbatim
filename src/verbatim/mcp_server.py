"""MCP server — exposes Verbatim's state graph as tools any agent client can call.

Run with `verbatim-mcp` (stdio transport). Configure your client (Claude Code,
Cursor, etc.) to launch this command as an MCP server and the tools listed
below become available to the agent.

Tools exposed:
- list_commitments    — open commitments, optionally filtered by actor / confidence
- list_decisions      — past decisions, optionally filtered by confidence
- list_open_questions — unresolved questions, optionally filtered by raised-by
- list_blockers       — current blockers, optionally filtered by owner
- recent_sessions     — recent extraction sessions
- verbatim_stats      — quick counts of open items
- resolve_entity      — mark an entity as resolved
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__, state

server: Server = Server("verbatim")


# ----------------------- tool definitions -----------------------


_CONFIDENCE_ENUM = {"type": "string", "enum": ["low", "medium", "high"]}


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_commitments",
            description=(
                "List open commitments — things people on the team have agreed to deliver. "
                "Each result includes the actor, deliverable, deadline (if stated), "
                "confidence, and the verbatim quote that supports the extraction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Filter to commitments by this person (case-insensitive name match).",
                    },
                    "min_confidence": {
                        **_CONFIDENCE_ENUM,
                        "description": "Minimum confidence level to include.",
                    },
                    "include_resolved": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include commitments already marked resolved.",
                    },
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
        ),
        types.Tool(
            name="list_decisions",
            description=(
                "List decisions the team has made. Each result includes the topic, "
                "outcome, participants, rationale (if stated), and the supporting quote."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_confidence": _CONFIDENCE_ENUM,
                    "include_resolved": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
        ),
        types.Tool(
            name="list_open_questions",
            description=(
                "List unresolved questions raised in past meetings or threads — "
                "things that need answers but haven't been resolved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "raised_by": {"type": "string"},
                    "min_confidence": _CONFIDENCE_ENUM,
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
        ),
        types.Tool(
            name="list_blockers",
            description="List current blockers — work that can't progress until something is resolved.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "min_confidence": _CONFIDENCE_ENUM,
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
        ),
        types.Tool(
            name="recent_sessions",
            description="List recent extraction sessions — useful for finding the source of a particular entity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                },
            },
        ),
        types.Tool(
            name="verbatim_stats",
            description="Quick counts: how many sessions are ingested, how many open items per kind.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="resolve_entity",
            description=(
                "Mark an entity (commitment/decision/question/blocker) as resolved so it "
                "stops appearing in default queries. Pass the full or prefix entity id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Full entity id or unique 8+ char prefix.",
                    },
                },
                "required": ["entity_id"],
            },
        ),
    ]


# ----------------------- tool dispatch -----------------------


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    args = arguments or {}
    conn = state.open_db()
    try:
        if name == "list_commitments":
            items = state.list_commitments(
                conn,
                actor=args.get("actor"),
                min_confidence=args.get("min_confidence"),
                status=None if args.get("include_resolved") else "open",
                limit=int(args.get("limit", 50)),
            )
            text = _format_entities(items, kind="commitment")
        elif name == "list_decisions":
            items = state.list_decisions(
                conn,
                min_confidence=args.get("min_confidence"),
                status=None if args.get("include_resolved") else "open",
                limit=int(args.get("limit", 50)),
            )
            text = _format_entities(items, kind="decision")
        elif name == "list_open_questions":
            items = state.list_open_questions(
                conn,
                raised_by=args.get("raised_by"),
                min_confidence=args.get("min_confidence"),
                limit=int(args.get("limit", 50)),
            )
            text = _format_entities(items, kind="open_question")
        elif name == "list_blockers":
            items = state.list_blockers(
                conn,
                owner=args.get("owner"),
                min_confidence=args.get("min_confidence"),
                limit=int(args.get("limit", 50)),
            )
            text = _format_entities(items, kind="blocker")
        elif name == "recent_sessions":
            sessions = state.recent_sessions(conn, limit=int(args.get("limit", 20)))
            text = _format_sessions(sessions)
        elif name == "verbatim_stats":
            s = state.stats(conn)
            text = json.dumps(s, indent=2)
        elif name == "resolve_entity":
            full_id = _resolve_prefix(conn, args["entity_id"])
            if full_id is None:
                text = f"No entity matched id prefix '{args['entity_id']}'."
            else:
                ok = state.resolve_entity(conn, full_id)
                text = f"Resolved {full_id}." if ok else f"No change for {full_id}."
        else:
            text = f"Unknown tool: {name}"
    finally:
        conn.close()
    return [types.TextContent(type="text", text=text)]


# ----------------------- formatting -----------------------


def _format_entities(items: list[dict[str, Any]], *, kind: str) -> str:
    if not items:
        return f"No {kind.replace('_', ' ')}s matched."

    lines: list[str] = [f"{len(items)} {kind.replace('_', ' ')}(s):", ""]
    for it in items:
        p = it["payload"]
        srcs = it["sources"]
        head = f"- [{it['confidence']}] id={it['id'][:8]}"
        if kind == "commitment":
            deadline = f" by {p['deadline']}" if p.get("deadline") else ""
            to = f" → {p['to']}" if p.get("to") else ""
            head += f"  **{p.get('actor', '?')}**{to} — {p.get('deliverable', '?')}{deadline}"
        elif kind == "decision":
            head += f"  **{p.get('topic', '?')}** → {p.get('outcome', '?')}"
            if p.get("participants"):
                head += f"  (participants: {', '.join(p['participants'])})"
        elif kind == "open_question":
            raised = f" raised by {p['raised_by']}" if p.get("raised_by") else ""
            addressed = f" → {p['addressed_to']}" if p.get("addressed_to") else ""
            head += f"  {p.get('question') or p.get('topic', '?')}{raised}{addressed}"
        elif kind == "blocker":
            owner = f" (owner: {p['owner']})" if p.get("owner") else ""
            head += f"  **{p.get('blocked_thing', '?')}** blocked by **{p.get('blocked_by', '?')}**{owner}"
        lines.append(head)
        for s in srcs:
            spk = f"{s['speaker']}: " if s.get("speaker") else ""
            ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
            lines.append(f"    > {ts}{spk}{s['verbatim_quote']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_sessions(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "No sessions ingested yet."
    lines = [f"{len(sessions)} session(s):", ""]
    for s in sessions:
        lines.append(
            f"- id={s['id'][:8]}  "
            f"at={s['extracted_at'][:19]}  "
            f"source={s['source_path'] or '<stdin>'}  "
            f"model={s['model']}  "
            f"items={s['entity_count']}"
        )
        if s.get("meeting_summary"):
            lines.append(f"    summary: {s['meeting_summary'][:200]}")
    return "\n".join(lines)


def _resolve_prefix(conn, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT id FROM entities WHERE id LIKE ? LIMIT 2",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


# ----------------------- entrypoint -----------------------


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="verbatim",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:  # console script entry point
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
