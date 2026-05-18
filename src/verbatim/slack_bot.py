"""Slack bot — the consumer-facing surface for Verbatim.

This module is the answer to "do non-technical users have to live in a terminal?".
The CLI stays the operator surface. The bot is how everyone else interacts with
Verbatim state — slash commands to query, posted digests to surface, all from
inside Slack where the team already lives.

# Transport

Uses Slack Socket Mode (`SocketModeClient`). The bot opens an outbound WebSocket
to Slack and reads events off it. **No public URL or webhook endpoint is needed.**
This is what makes the bot deployable on a laptop, a self-hosted server, or
anywhere with outbound internet — no nginx, no Cloudflare tunnel, no ngrok.

# Auth

Two tokens are required:

- **Bot Token** (`xoxb-...`): for posting messages and reading users. Same token
  you use for `ingest-slack-api`.
- **App-Level Token** (`xapp-...`): for the Socket Mode connection. Generate
  this in your Slack App settings → Basic Information → App-Level Tokens, with
  the `connections:write` scope.

# Slash commands

Register `/verbatim` in your Slack App (Settings → Slash Commands → Create New
Command). The bot supports these sub-commands in the text argument:

```
/verbatim commitments [actor]
/verbatim decisions
/verbatim questions
/verbatim blockers [owner]
/verbatim stats
/verbatim show <id-prefix>
/verbatim help
```

Responses are ephemeral by default (only the invoker sees them) to keep channels
quiet. We could add `share` modifiers later for in-channel responses.

# Digest

`post_digest(channel_id)` pushes the same summary into a channel publicly. Use
`verbatim slack-bot digest --channel ...` from cron, or after ingest runs.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from . import state

log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 20


# ----------------------- command parsing -----------------------


@dataclass
class ParsedCommand:
    """The result of parsing the `text` argument of a /verbatim slash command."""

    subcommand: str
    args: list[str]
    error: str | None = None


def parse_command_text(text: str) -> ParsedCommand:
    """Parse the free-form text after /verbatim into a (subcommand, args) shape.

    The grammar is forgiving — `text` may be empty (treated as 'help'), or any
    whitespace-separated sequence. We don't accept flags; the bot's UX is
    intentionally narrower than the CLI.
    """
    tokens = text.strip().split() if text else []
    if not tokens:
        return ParsedCommand(subcommand="help", args=[])
    sub = tokens[0].lower()
    if sub in {"open-questions", "open_questions", "open"}:
        sub = "questions"
    return ParsedCommand(subcommand=sub, args=tokens[1:])


# ----------------------- formatters (Slack mrkdwn) -----------------------

# Slack's mrkdwn uses *bold*, _italic_, `code`, > quote, and limited Block Kit.
# We keep it text-only here for portability; richer Block Kit later if needed.


def format_help() -> str:
    return (
        "*Verbatim* — query your team's operational state.\n\n"
        "• `/verbatim commitments [actor]` — open commitments\n"
        "• `/verbatim decisions` — recent decisions\n"
        "• `/verbatim questions` — unresolved questions\n"
        "• `/verbatim blockers [owner]` — current blockers\n"
        "• `/verbatim stats` — counts overview\n"
        "• `/verbatim show <id-prefix>` — entity detail with sources\n"
    )


def format_commitments(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No open commitments._"
    lines = [f"*{len(items)} open commitment(s):*", ""]
    for it in items:
        p = it["payload"]
        deadline = f" *by* {p['deadline']}" if p.get("deadline") else ""
        to = f" → {p['to']}" if p.get("to") else ""
        conf = _conf_emoji(it["confidence"])
        merged = f" _(merged from {it.get('merged_count', 0)+1} sources)_" if it.get("merged_count") else ""
        lines.append(
            f"{conf} *{p.get('actor') or '?'}*{to} — {p.get('deliverable') or '?'}{deadline}{merged}"
        )
        lines.append(f"      _id: `{it['id'][:10]}…`_")
    return "\n".join(lines)


def format_decisions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No decisions recorded._"
    lines = [f"*{len(items)} decision(s):*", ""]
    for it in items:
        p = it["payload"]
        conf = _conf_emoji(it["confidence"])
        merged = f" _(merged from {it.get('merged_count', 0)+1} sources)_" if it.get("merged_count") else ""
        lines.append(f"{conf} *{p.get('topic') or '?'}* → {p.get('outcome') or '?'}{merged}")
        if p.get("rationale"):
            lines.append(f"      _{p['rationale']}_")
        lines.append(f"      _id: `{it['id'][:10]}…`_")
    return "\n".join(lines)


def format_questions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No unresolved questions._"
    lines = [f"*{len(items)} open question(s):*", ""]
    for it in items:
        p = it["payload"]
        conf = _conf_emoji(it["confidence"])
        addressed = f" → *{p['addressed_to']}*" if p.get("addressed_to") else ""
        lines.append(
            f"{conf} *{p.get('topic') or '?'}* — {p.get('question') or '?'}"
            f"{addressed}  _(raised by {p.get('raised_by') or '?'})_"
        )
        lines.append(f"      _id: `{it['id'][:10]}…`_")
    return "\n".join(lines)


def format_blockers(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No blockers._"
    lines = [f"*{len(items)} blocker(s):*", ""]
    for it in items:
        p = it["payload"]
        conf = _conf_emoji(it["confidence"])
        owner = f"  *owner*: {p['owner']}" if p.get("owner") else ""
        lines.append(
            f"{conf} *{p.get('blocked_thing') or '?'}* blocked by *{p.get('blocked_by') or '?'}*{owner}"
        )
        lines.append(f"      _id: `{it['id'][:10]}…`_")
    return "\n".join(lines)


def format_stats(stats_dict: dict[str, int]) -> str:
    return (
        f"*Verbatim state* ({stats_dict.get('sessions', 0)} sessions ingested)\n"
        f"• {stats_dict.get('commitments_open', 0)} open commitments\n"
        f"• {stats_dict.get('decisions_open', 0)} decisions\n"
        f"• {stats_dict.get('open_questions_open', 0)} unresolved questions\n"
        f"• {stats_dict.get('blockers_open', 0)} blockers\n"
        f"• {stats_dict.get('entities_merged', 0)} entities merged via reconciliation\n"
        f"• {stats_dict.get('projections_active', 0)} active projections to external trackers"
    )


def format_entity_detail(entity: dict[str, Any]) -> str:
    """Detail view for /verbatim show <id-prefix>."""
    p = entity["payload"]
    conf = _conf_emoji(entity["confidence"])
    lines: list[str] = [
        f"{conf} *{entity['kind']}*  `{entity['id']}`",
    ]
    if entity["kind"] == "commitment":
        deadline = f"  *by* {p['deadline']}" if p.get("deadline") else ""
        lines.append(f"*{p.get('actor') or '?'}* — {p.get('deliverable') or '?'}{deadline}")
    elif entity["kind"] == "decision":
        lines.append(f"*{p.get('topic') or '?'}* → {p.get('outcome') or '?'}")
        if p.get("rationale"):
            lines.append(f"_rationale: {p['rationale']}_")
    elif entity["kind"] == "open_question":
        lines.append(f"*{p.get('topic') or '?'}* — {p.get('question') or '?'}")
        if p.get("raised_by"):
            lines.append(f"_raised by {p['raised_by']}_")
    elif entity["kind"] == "blocker":
        lines.append(f"*{p.get('blocked_thing') or '?'}* blocked by *{p.get('blocked_by') or '?'}*")

    if entity.get("merged_count"):
        lines.append(f"_(merged from {entity['merged_count']+1} sources)_")
    lines.append("")
    lines.append("*Supporting quotes:*")
    for s in entity.get("sources", []):
        speaker = f"{s.get('speaker')}: " if s.get("speaker") else ""
        ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
        lines.append(f"> {ts}{speaker}{s['verbatim_quote']}")
    return "\n".join(lines)


def _conf_emoji(confidence: str) -> str:
    return {"high": ":large_green_circle:", "medium": ":large_yellow_circle:", "low": ":red_circle:"}.get(
        confidence, ":white_circle:"
    )


# ----------------------- dispatch -----------------------


def dispatch_command(parsed: ParsedCommand, conn: sqlite3.Connection) -> str:
    """Route a parsed subcommand to the right state query, return Slack mrkdwn."""
    sub = parsed.subcommand

    if sub == "help":
        return format_help()
    if sub == "stats":
        return format_stats(state.stats(conn))
    if sub == "commitments":
        actor = parsed.args[0] if parsed.args else None
        items = state.list_commitments(conn, actor=actor, limit=DEFAULT_LIST_LIMIT)
        return format_commitments(items)
    if sub == "decisions":
        items = state.list_decisions(conn, limit=DEFAULT_LIST_LIMIT)
        return format_decisions(items)
    if sub == "questions":
        items = state.list_open_questions(conn, limit=DEFAULT_LIST_LIMIT)
        return format_questions(items)
    if sub == "blockers":
        owner = parsed.args[0] if parsed.args else None
        items = state.list_blockers(conn, owner=owner, limit=DEFAULT_LIST_LIMIT)
        return format_blockers(items)
    if sub == "show":
        if not parsed.args:
            return "_Usage: `/verbatim show <id-prefix>`_"
        prefix = parsed.args[0]
        full_id = _resolve_id_prefix(conn, prefix)
        if full_id is None:
            return f"_No entity matches id prefix `{prefix}`._"
        entity = state.show_entity(conn, full_id)
        if entity is None:
            return f"_Entity not found: `{full_id}`._"
        return format_entity_detail(entity)

    return f"_Unknown subcommand `{sub}`._\n\n{format_help()}"


def _resolve_id_prefix(conn: sqlite3.Connection, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT id FROM entities WHERE id LIKE ? LIMIT 2",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


# ----------------------- bot -----------------------


class VerbatimSlackBot:
    """Slack Socket Mode bot wrapper.

    Construct with bot + app tokens, then call `run()` to block forever, or
    `post_digest(channel_id)` for a one-shot publish.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        db_path: Path | None = None,
        web_client: WebClient | None = None,
        socket_client: SocketModeClient | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required (xoxb-... — set SLACK_BOT_TOKEN).")
        if not app_token and socket_client is None:
            raise ValueError("app_token is required for Socket Mode (xapp-... — set SLACK_APP_TOKEN).")
        self._web = web_client or WebClient(token=bot_token)
        self._socket = socket_client or SocketModeClient(app_token=app_token, web_client=self._web)
        self._db_path = db_path

    # ----- inbound: slash commands -----

    def _on_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        """Slack Socket Mode callback. Ack quickly, then process."""
        try:
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            if req.type == "slash_commands":
                self._handle_slash_command(req.payload or {})
            # Other event types ignored for v1
        except Exception:  # noqa: BLE001
            log.exception("slack bot failed handling request")

    def _handle_slash_command(self, payload: dict[str, Any]) -> None:
        text = payload.get("text") or ""
        response_url = payload.get("response_url")
        channel_id = payload.get("channel_id")
        if not response_url and not channel_id:
            log.warning("slash command had no response_url or channel_id: %r", payload)
            return

        parsed = parse_command_text(text)
        conn = state.open_db(self._db_path)
        try:
            body = dispatch_command(parsed, conn)
        finally:
            conn.close()

        # Use the ephemeral chat.postEphemeral so only the invoker sees the reply.
        # Falls back to chat.postMessage in-channel if user_id is missing.
        user_id = payload.get("user_id")
        if user_id and channel_id:
            self._web.chat_postEphemeral(channel=channel_id, user=user_id, text=body)
        elif channel_id:
            self._web.chat_postMessage(channel=channel_id, text=body)

    # ----- run loop -----

    def run(self) -> None:
        """Connect to Slack and block forever."""
        self._socket.socket_mode_request_listeners.append(self._on_request)
        self._socket.connect()
        # Block by waiting on the socket. Use an Event the operator can SIGINT.
        import threading
        threading.Event().wait()

    # ----- outbound: digests -----

    def post_digest(self, channel: str) -> dict[str, Any]:
        """Post a summary digest of current state into the given channel."""
        conn = state.open_db(self._db_path)
        try:
            body = self._render_digest(conn)
        finally:
            conn.close()
        return self._web.chat_postMessage(channel=channel, text=body).data  # type: ignore[no-any-return]

    def _render_digest(self, conn: sqlite3.Connection) -> str:
        stats_dict = state.stats(conn)
        recent_commitments = state.list_commitments(conn, limit=5)
        recent_blockers = state.list_blockers(conn, limit=5)

        sections = [
            "*:bookmark_tabs: Verbatim digest*",
            "",
            format_stats(stats_dict),
            "",
        ]
        if recent_commitments:
            sections.append("*Recent commitments:*")
            for it in recent_commitments:
                p = it["payload"]
                deadline = f" *by* {p['deadline']}" if p.get("deadline") else ""
                conf = _conf_emoji(it["confidence"])
                sections.append(f"{conf} *{p.get('actor') or '?'}* — {p.get('deliverable') or '?'}{deadline}")
            sections.append("")
        if recent_blockers:
            sections.append("*Open blockers:*")
            for it in recent_blockers:
                p = it["payload"]
                conf = _conf_emoji(it["confidence"])
                sections.append(
                    f"{conf} *{p.get('blocked_thing') or '?'}* blocked by *{p.get('blocked_by') or '?'}*"
                )
        return "\n".join(sections).rstrip()
