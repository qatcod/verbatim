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
- ingest_transcript   — extract + persist from transcript text (idempotent via external_id)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import weakref
from typing import Any

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.context import ServerRequestContext
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__, state, store

# ----------------------- tool definitions -----------------------
#
# mcp 2.x registers handlers through the `Server` constructor (`on_list_tools=`/`on_call_tool=`),
# not the 1.x `@server.list_tools()`/`@server.call_tool()` decorators, which it removed outright
# (SEP-2577, 2026-07-28). So the handlers are defined first and the `server` instance is built from
# them below — same tools, same arguments, same returned text, registered the way 2.x reads them.


_CONFIDENCE_ENUM = {"type": "string", "enum": ["low", "medium", "high"]}


async def _list_tools(
    _ctx: ServerRequestContext[Any],
    _params: types.PaginatedRequestParams | None = None,
) -> types.ListToolsResult:
    tools = [
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
        types.Tool(
            name="ingest_transcript",
            description=(
                "Extract commitments, decisions, open questions and blockers from a "
                "transcript and persist them to the state graph. Pass `external_id` (your "
                "own id for this transcript) to make retries safe: a repeat returns the "
                "first run's session_id and counts without re-extracting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The transcript text.",
                    },
                    "external_id": {
                        "type": "string",
                        "description": (
                            "Caller's own id for this transcript. Makes the ingest "
                            "idempotent. Strongly recommended for automated callers."
                        ),
                    },
                    "source_label": {
                        "type": "string",
                        "description": "Human-readable origin, recorded on the session.",
                    },
                },
                "required": ["text"],
            },
        ),
    ]
    return types.ListToolsResult(tools=tools)


# ----------------------- ingestion -----------------------
#
# Ingestion is the one WRITE tool, and it is different from the seven read tools in two ways that
# shape all of the code below: it makes a paid LLM call that takes tens of seconds, and it is called
# by automation that retries.

# One extraction at a time per process.
#
# verbatim's extraction runs against a host with FOUR slots that a voice turn also uses, and a long
# transcript has a large prefill. Two concurrent extractions take two of those slots; four take all
# of them. The limit lives HERE rather than in the calling application because this is where the LLM
# call is made -- so it holds for every client, including a human running `verbatim ingest` during a
# live meeting, which a queue in one caller cannot stop.
#
# IN-PROCESS, and that is a real limit rather than an oversight: it serialises one container and
# nothing more. Two verbatim replicas would each admit one extraction concurrently. Single-container
# is the shipped topology (see the Dockerfile), and this comment is the thing to read first on the
# day that stops being true -- at which point the lease belongs in the database, not in a mutex.
#
# Keyed by event loop, NOT a single module-level Lock, and that is a correctness fix rather than a
# style choice. `asyncio.Lock()` binds to the loop that first awaits it, and raises
# `RuntimeError: ... is bound to a different event loop` on any later loop. A single module-level
# lock therefore works only while exactly one loop ever exists in the process -- true for
# `uvicorn.run` and true for the stdio `asyncio.run`, and false the moment anything runs a second
# loop. A WeakKeyDictionary so a finished loop's entry is collected with the loop instead of pinning
# it for the life of the process.
_INGEST_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _ingest_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _INGEST_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _INGEST_LOCKS[loop] = lock
    return lock


def _format_ingest(session_id: str, counts: dict[str, int], *, replayed: bool) -> str:
    total = sum(counts.values())
    head = "Already ingested" if replayed else f"Ingested {total} item(s)"
    # Zero counts are printed, not omitted. "Extraction returned nothing" is an ORDINARY outcome for
    # a meeting with no commitments, and the caller has to be able to tell it from a failure -- so it
    # reads as explicit zeros rather than as an absence that looks like something went wrong.
    return (
        f"{head} for session {session_id}.\n"
        f"commitments: {counts['commitment']}\n"
        f"decisions: {counts['decision']}\n"
        f"open questions: {counts['open_question']}\n"
        f"blockers: {counts['blocker']}"
    )


async def _ingest_transcript(conn, args: dict[str, Any]) -> str:
    """Extract from `text` and persist, idempotently when `external_id` is given.

    Arguments are validated by hand because mcp 2.x does NOT validate against the tool's
    `inputSchema` -- the same gap documented on `_call_tool` for exceptions. Without this, a
    `text` of `None` reaches `extract()` and fails somewhere far from the cause.
    """
    # Imported here, not at module scope: the extractor pulls in the model-backend stack, and the
    # seven read-only tools must not pay for it. stdio startup is on the critical path of every
    # client launch.
    from .extractor import extract

    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("ingest_transcript requires a non-empty 'text'")
    external_id = args.get("external_id")
    if external_id is not None and not isinstance(external_id, str):
        raise ValueError("'external_id' must be a string when given")
    source_label = args.get("source_label")
    if source_label is not None and not isinstance(source_label, str):
        raise ValueError("'source_label' must be a string when given")

    def _replay() -> str | None:
        """The first run's answer, if this external_id already landed one."""
        if not external_id:
            return None
        row = store.find_session_by_external_id(conn, external_id)
        if row is None:
            return None
        return _format_ingest(
            row["id"], store.count_entities_by_kind(conn, row["id"]), replayed=True
        )

    # Checked BEFORE taking the lock, so a replay costs neither the wait nor the model call.
    hit = _replay()
    if hit is not None:
        return hit

    async with _ingest_lock():
        # Checked AGAIN inside the lock. Two calls carrying the same external_id can both pass the
        # pre-check and queue here; without this the second would re-extract and then collide on the
        # unique index. This turns that into the replay it should have been.
        hit = _replay()
        if hit is not None:
            return hit

        # Only the LLM call goes to a thread. `conn` CANNOT: sqlite3.connect defaults to
        # check_same_thread=True (store.connect does not override it), so touching it off the loop
        # thread raises ProgrammingError -- and it would do so only under the HTTP transport, where
        # the loop is shared. save_extraction is a handful of INSERTs and stays on this thread.
        result, diag = await asyncio.to_thread(extract, text)

        try:
            summary = state.save_extraction(
                conn,
                result,
                diag,
                source_path=source_label,
                external_id=external_id,
            )
        except sqlite3.IntegrityError:
            # The unique index fired: another PROCESS ingested this external_id while this
            # extraction was running. The lock above cannot see across processes. The caller's answer
            # is still the first run's result, not an error -- the work is done, just not by us.
            hit = _replay()
            if hit is not None:
                return hit
            raise

    return _format_ingest(summary.session_id, summary.counts, replayed=False)


# ----------------------- tool dispatch -----------------------


async def _call_tool(
    _ctx: ServerRequestContext[Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    name = params.name
    args = params.arguments or {}
    conn = state.open_db()
    try:
        # ingest_transcript is handled here rather than in _dispatch_tool because it is the only
        # tool that must AWAIT (its extraction is offloaded so a tens-of-seconds model call does not
        # block the event loop -- which under HTTP would stall every other request). _dispatch_tool
        # stays synchronous for the seven read tools, which are microsecond SQLite reads. Inside the
        # same try, so the isError handling below covers it identically.
        if name == "ingest_transcript":
            text = await _ingest_transcript(conn, args)
        else:
            text = _dispatch_tool(conn, name, args)
    except Exception as exc:
        # mcp 1.x's @server.call_tool() decorator caught handler exceptions and returned them as
        # CallToolResult(isError=True, ...) -- a tool-level error the *model* gets to see and can
        # recover from (retry with different arguments, tell the user what's wrong). mcp 2.x's
        # on_call_tool= constructor kwarg does none of that: an uncaught exception here becomes an
        # MCPError the *client* raises instead, which a caller like `resolve_entity {}` (missing
        # the required entity_id) turns into a hard failure instead of a message the model could
        # act on. Restore the 1.x behaviour explicitly rather than depend on mcp to do it.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error calling tool '{name}': {exc}")],
            isError=True,
        )
    finally:
        conn.close()
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _dispatch_tool(conn, name: str, args: dict[str, Any]) -> str:
    """The actual per-tool branch, split out so `_call_tool` can wrap all of it in one try/except."""
    if name == "list_commitments":
        items = state.list_commitments(
            conn,
            actor=args.get("actor"),
            min_confidence=args.get("min_confidence"),
            status=None if args.get("include_resolved") else "open",
            limit=int(args.get("limit", 50)),
        )
        return _format_entities(items, kind="commitment")
    elif name == "list_decisions":
        items = state.list_decisions(
            conn,
            min_confidence=args.get("min_confidence"),
            status=None if args.get("include_resolved") else "open",
            limit=int(args.get("limit", 50)),
        )
        return _format_entities(items, kind="decision")
    elif name == "list_open_questions":
        items = state.list_open_questions(
            conn,
            raised_by=args.get("raised_by"),
            min_confidence=args.get("min_confidence"),
            limit=int(args.get("limit", 50)),
        )
        return _format_entities(items, kind="open_question")
    elif name == "list_blockers":
        items = state.list_blockers(
            conn,
            owner=args.get("owner"),
            min_confidence=args.get("min_confidence"),
            limit=int(args.get("limit", 50)),
        )
        return _format_entities(items, kind="blocker")
    elif name == "recent_sessions":
        sessions = state.recent_sessions(conn, limit=int(args.get("limit", 20)))
        return _format_sessions(sessions)
    elif name == "verbatim_stats":
        s = state.stats(conn)
        return json.dumps(s, indent=2)
    elif name == "resolve_entity":
        full_id = _resolve_prefix(conn, args["entity_id"])
        if full_id is None:
            return f"No entity matched id prefix '{args['entity_id']}'."
        ok = state.resolve_entity(conn, full_id)
        return f"Resolved {full_id}." if ok else f"No change for {full_id}."
    else:
        return f"Unknown tool: {name}"


# ----------------------- server instance -----------------------
#
# Built after the handlers because 2.x takes them as constructor kwargs. `mcp_http.build_app`
# imports this exact instance, so stdio and HTTP serve the same tools from the same registration.
server: Server = Server(
    "verbatim",
    version=__version__,
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)


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


def parse_transport_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Transport selection for `verbatim-mcp`.

    stdio is the DEFAULT and must stay so: Claude Code and Cursor already invoke this with no
    arguments, and changing that would break every existing configuration silently.
    """
    p = argparse.ArgumentParser(prog="verbatim-mcp")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--port", type=int, default=8081)
    # Local-only by default: someone running `verbatim-mcp --transport http` on a laptop to try it
    # out should not bind the LAN by default. Binding every interface is the Dockerfile's job, not
    # this default's -- the Dockerfile CMD passes --host 0.0.0.0 explicitly, where that is the point.
    p.add_argument("--host", default="127.0.0.1")
    return p.parse_args(argv)


def main() -> None:  # console script entry point
    args = parse_transport_args()
    if args.transport == "stdio":
        asyncio.run(_run())
        return

    import uvicorn

    from .mcp_http import build_app

    token = os.environ.get("VERBATIM_MCP_TOKEN", "")
    # build_app raises on an empty token. Let it: a service that serves a team's state anonymously
    # because an env var was unset is worse than one that refuses to start and says why.
    uvicorn.run(build_app(token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
