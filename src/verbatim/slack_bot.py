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

# Replying to slash commands

Slack provides a one-shot `response_url` with every slash-command invocation
that's valid for 30 minutes. Posting JSON to it delivers the reply as an
ephemeral message to the invoker — **without requiring the bot to be a member
of the channel**. We use that path instead of `chat.postEphemeral`, which
returns `not_in_channel` whenever someone runs `/verbatim` in a channel the
bot hasn't been invited to. This makes the bot work in any channel by default.

# Auth

Two tokens are required:

- **Bot Token** (`xoxb-...`): for posting messages (digest command).
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

import httpx
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from . import ask, simplify, state, store

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
        "• `/verbatim ask <question>` — ask in plain English\n"
        "• `/verbatim simplify <id-prefix>` — explain an item, jargon-free\n"
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
        merged = _merged_pill_text(it.get("merged_count", 0))
        lines.append(
            f"{conf} *{p.get('actor') or '?'}*{to} — {p.get('deliverable') or '?'}{deadline}{merged}"
        )
        lines.append(f"      _id: `{it['id'][:8]}…`_")
    return "\n".join(lines)


def format_decisions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No decisions recorded._"
    lines = [f"*{len(items)} decision(s):*", ""]
    for it in items:
        p = it["payload"]
        conf = _conf_emoji(it["confidence"])
        merged = _merged_pill_text(it.get("merged_count", 0))
        lines.append(f"{conf} *{p.get('topic') or '?'}* → {p.get('outcome') or '?'}{merged}")
        if p.get("rationale"):
            lines.append(f"      _{p['rationale']}_")
        lines.append(f"      _id: `{it['id'][:8]}…`_")
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
        lines.append(f"      _id: `{it['id'][:8]}…`_")
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
        lines.append(f"      _id: `{it['id'][:8]}…`_")
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
        n = entity["merged_count"]
        word = "source" if n == 1 else "sources"
        lines.append(f"_(merged with {n} other {word})_")
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


# ----------------------- Block Kit (interactive HITL) -----------------------


_KIND_LABEL = {
    "commitment": "Commitment",
    "decision": "Decision",
    "open_question": "Question",
    "blocker": "Blocker",
}


def _result_summary(entity: dict[str, Any]) -> str:
    p = entity.get("payload", {})
    k = entity["kind"]
    if k == "commitment":
        actor = p.get("actor") or "?"
        return f"{actor} → {p.get('deliverable') or '?'}"
    if k == "decision":
        return f"{p.get('topic') or '?'} → {p.get('outcome') or '?'}"
    if k == "open_question":
        return p.get("question") or p.get("topic") or "?"
    if k == "blocker":
        return f"{p.get('blocked_thing') or '?'} blocked by {p.get('blocked_by') or '?'}"
    return entity["id"]


def build_extraction_card_blocks(entity: dict[str, Any]) -> list[dict[str, Any]]:
    """Block Kit payload for the interactive extraction card.

    Mirrors the design's mock from `docs/design/verbatim.html`: header chip with
    the verbatim lock label, a quote section, then four buttons. action_id
    format is `verbatim:<verb>:<entity_id>` so the handler can dispatch.
    """
    kind = entity["kind"]
    kind_label = _KIND_LABEL.get(kind, kind.replace("_", " ").title())
    eid = entity["id"]
    short_id = f"VRB-{eid[:8]}"
    conf = entity.get("confidence", "unknown")

    summary = _result_summary(entity)
    sources = entity.get("sources") or []
    quote_text = sources[0].get("verbatim_quote") if sources else ""
    speaker = (sources[0].get("speaker") if sources else None) or ""

    status = entity.get("status") or "open"
    is_resolved_state = status in {"confirmed", "dismissed", "resolved"}

    header_text = (
        f"*{_conf_emoji(conf)} verbatim* — extracted *{kind_label}* "
        f"`{short_id}` _( {conf} confidence )_"
    )
    if is_resolved_state:
        header_text += f"  ·  *status:* `{status}`"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape_mrkdwn(summary)}*"}},
    ]
    if quote_text:
        attribution = f" — _{_escape_mrkdwn(speaker)}_" if speaker else ""
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {_escape_mrkdwn(quote_text)}{attribution}",
            },
        })

    if not is_resolved_state:
        blocks.append({
            "type": "actions",
            "block_id": f"verbatim_actions_{eid}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✓ Confirm"},
                    "action_id": f"verbatim:confirm:{eid}",
                    "value": eid,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✕ Not a commitment"},
                    "action_id": f"verbatim:dismiss:{eid}",
                    "value": eid,
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit details"},
                    "action_id": f"verbatim:edit:{eid}",
                    "value": eid,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reassign"},
                    "action_id": f"verbatim:reassign:{eid}",
                    "value": eid,
                },
            ],
        })
    return blocks


def _escape_mrkdwn(text: str | None) -> str:
    """Defensive escape for Slack mrkdwn. We treat user content as plain text."""
    if not text:
        return ""
    # Slack mrkdwn escapes via &amp; &lt; &gt;
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def dispatch_action(
    conn,
    *,
    verb: str,
    entity_id: str,
    user_id: str | None,
) -> str:
    """Handle one button click by `verb`. Returns the ephemeral reply text.

    Confirm and Dismiss mutate state and write an audit row. Edit and
    Reassign no longer mutate state directly — they tell the caller to open
    a modal (returning text confirms the click was received; the actual
    field change comes through `apply_edit` / `apply_reassign` once the
    operator submits the modal).
    """
    entity = state.show_entity(conn, entity_id)
    if entity is None:
        return f"_Entity `{entity_id[:8]}…` not found — it may have been deleted._"

    if verb == "confirm":
        store.update_entity_status(conn, entity_id, "confirmed")
        store.record_audit(
            conn, entity_id=entity_id, action="confirm",
            actor_id=user_id, actor_label=f"<@{user_id}>" if user_id else None,
            before={"status": "open"}, after={"status": "confirmed"},
        )
        actor = (entity.get("payload") or {}).get("actor") or "owner"
        attribution = f" by <@{user_id}>" if user_id else ""
        return (
            f":white_check_mark: Confirmed{attribution}. "
            f"`VRB-{entity_id[:8]}` is now marked as a confirmed *{entity['kind']}* "
            f"({actor})."
        )
    if verb == "dismiss":
        store.update_entity_status(conn, entity_id, "dismissed")
        store.record_audit(
            conn, entity_id=entity_id, action="dismiss",
            actor_id=user_id, actor_label=f"<@{user_id}>" if user_id else None,
            before={"status": "open"}, after={"status": "dismissed"},
        )
        return (
            f":x: Dismissed `VRB-{entity_id[:8]}` — it won't appear in active "
            "queries. Run `verbatim unlink` or update via the web UI if this was "
            "an accident."
        )
    if verb == "edit":
        return (
            f"_Opening edit form for `VRB-{entity_id[:8]}`…_ "
            "If the modal doesn't appear, your Slack app may be missing the "
            "`views.open` permission — see verbatim slack-bot setup docs."
        )
    if verb == "reassign":
        return (
            f"_Opening reassign picker for `VRB-{entity_id[:8]}`…_ "
            "If the modal doesn't appear, your Slack app may be missing the "
            "`views.open` permission — see verbatim slack-bot setup docs."
        )
    return f"_Unknown action `{verb}` on `VRB-{entity_id[:8]}`._"


# ----------------------- modal builders + appliers (v0.10.1) -----------------------


def build_edit_modal_view(entity: dict[str, Any]) -> dict[str, Any]:
    """Build a Slack views.open modal view for editing one entity.

    The modal exposes the entity's natural-language fields based on its kind:
        commitment → deliverable, actor, deadline
        decision   → topic, outcome
        open_question → question, raised_by
        blocker    → blocked_thing, blocked_by, owner
    All inputs are optional — submitting with the original values is a no-op.
    A free-form `note` input lets the operator explain the edit (saved to audit).
    """
    kind = entity["kind"]
    eid = entity["id"]
    payload = entity.get("payload") or {}

    field_specs = _EDIT_FIELDS.get(kind, [])
    field_blocks: list[dict[str, Any]] = []
    for action_id, label, payload_key, multiline in field_specs:
        current = payload.get(payload_key) or ""
        field_blocks.append({
            "type": "input",
            "block_id": f"verbatim_edit_{action_id}",
            "label": {"type": "plain_text", "text": label},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": action_id,
                "initial_value": str(current),
                "multiline": bool(multiline),
            },
        })

    field_blocks.append({
        "type": "input",
        "block_id": "verbatim_edit_note",
        "label": {"type": "plain_text", "text": "Note (saved to audit log)"},
        "optional": True,
        "element": {
            "type": "plain_text_input",
            "action_id": "note",
            "multiline": True,
        },
    })

    return {
        "type": "modal",
        "callback_id": f"verbatim:edit:{eid}",
        "title": {"type": "plain_text", "text": "Edit · Verbatim"},
        "submit": {"type": "plain_text", "text": "Save changes"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": eid,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Editing *{_KIND_LABEL.get(kind, kind)}* "
                        f"`VRB-{eid[:8]}`. Empty fields are ignored."
                    ),
                },
            },
            {"type": "divider"},
            *field_blocks,
        ],
    }


def build_reassign_modal_view(entity: dict[str, Any]) -> dict[str, Any]:
    """Build a Slack views.open modal for reassigning one entity.

    Uses Slack's `users_select` so the operator picks a real workspace user.
    The picked user's display name is stored on `primary_actor`; the Slack
    user id is captured in the audit row as `actor_id` for later resolution.
    """
    eid = entity["id"]
    current = (entity.get("primary_actor") or "—")
    return {
        "type": "modal",
        "callback_id": f"verbatim:reassign:{eid}",
        "title": {"type": "plain_text", "text": "Reassign · Verbatim"},
        "submit": {"type": "plain_text", "text": "Reassign"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": eid,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Reassigning `VRB-{eid[:8]}` "
                        f"(*currently:* {_escape_mrkdwn(current)})."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "verbatim_reassign_user",
                "label": {"type": "plain_text", "text": "New owner"},
                "element": {
                    "type": "users_select",
                    "action_id": "user",
                    "placeholder": {"type": "plain_text", "text": "Pick a user"},
                },
            },
            {
                "type": "input",
                "block_id": "verbatim_reassign_name",
                "label": {"type": "plain_text", "text": "Display name override (optional)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "name",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Defaults to the picked Slack user's name.",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "verbatim_reassign_note",
                "label": {"type": "plain_text", "text": "Note (saved to audit log)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note",
                    "multiline": True,
                },
            },
        ],
    }


_EDIT_FIELDS: dict[str, list[tuple[str, str, str, bool]]] = {
    "commitment": [
        ("deliverable", "Deliverable", "deliverable", True),
        ("actor", "Actor", "actor", False),
        ("deadline", "Deadline", "deadline", False),
    ],
    "decision": [
        ("topic", "Topic", "topic", False),
        ("outcome", "Outcome", "outcome", True),
    ],
    "open_question": [
        ("question", "Question", "question", True),
        ("raised_by", "Raised by", "raised_by", False),
    ],
    "blocker": [
        ("blocked_thing", "Blocked thing", "blocked_thing", False),
        ("blocked_by", "Blocked by", "blocked_by", False),
        ("owner", "Owner", "owner", False),
    ],
}


def apply_edit(
    conn,
    *,
    entity_id: str,
    submitted_values: dict[str, str],
    user_id: str | None,
    user_label: str | None,
    note: str | None,
) -> str:
    """Apply edit-modal submission to an entity. Returns confirmation text."""
    entity = state.show_entity(conn, entity_id)
    if entity is None:
        return f"_Entity `{entity_id[:8]}…` not found._"

    kind = entity["kind"]
    field_specs = _EDIT_FIELDS.get(kind, [])
    payload_overrides: dict[str, Any] = {}
    new_actor: str | None = None
    new_topic: str | None = None
    new_deadline: str | None = None
    for action_id, _label, payload_key, _multiline in field_specs:
        val = submitted_values.get(action_id)
        if val is None or val == "":
            continue
        payload_overrides[payload_key] = val
        # Mirror denormalized columns where appropriate
        if kind == "commitment" and payload_key == "actor":
            new_actor = val
        elif kind == "open_question" and payload_key == "raised_by":
            new_actor = val
        elif kind == "blocker" and payload_key == "owner":
            new_actor = val
        elif payload_key == "topic":
            new_topic = val
        elif payload_key == "deadline":
            new_deadline = val

    snapshot = store.update_entity_fields(
        conn, entity_id,
        primary_actor=new_actor,
        primary_topic=new_topic,
        deadline=new_deadline,
        payload_overrides=payload_overrides or None,
    )
    if snapshot is None:
        return f"_Entity `{entity_id[:8]}…` could not be updated._"
    store.record_audit(
        conn, entity_id=entity_id, action="edit",
        actor_id=user_id,
        actor_label=f"<@{user_id}>" if user_id else None,
        before=snapshot["before"], after=snapshot["after"],
        note=note,
    )
    changed = ", ".join(payload_overrides.keys()) or "(no changes submitted)"
    attribution = f" by <@{user_id}>" if user_id else ""
    return (
        f":pencil: Edited{attribution} `VRB-{entity_id[:8]}` — updated {changed}."
    )


def apply_reassign(
    conn,
    *,
    entity_id: str,
    new_actor_label: str,
    slack_user_id: str | None,
    submitter_id: str | None,
    note: str | None,
) -> str:
    """Apply reassign-modal submission. Returns confirmation text."""
    entity = state.show_entity(conn, entity_id)
    if entity is None:
        return f"_Entity `{entity_id[:8]}…` not found._"

    kind = entity["kind"]
    # Mirror the new actor into the kind-specific payload field too so the
    # web UI and projections see a consistent record.
    payload_overrides: dict[str, Any] = {}
    if kind == "commitment":
        payload_overrides["actor"] = new_actor_label
    elif kind == "open_question":
        payload_overrides["raised_by"] = new_actor_label
    elif kind == "blocker":
        payload_overrides["owner"] = new_actor_label

    snapshot = store.update_entity_fields(
        conn, entity_id,
        primary_actor=new_actor_label,
        payload_overrides=payload_overrides or None,
    )
    if snapshot is None:
        return f"_Entity `{entity_id[:8]}…` could not be updated._"
    store.record_audit(
        conn, entity_id=entity_id, action="reassign",
        actor_id=submitter_id,
        actor_label=f"<@{submitter_id}>" if submitter_id else None,
        before=snapshot["before"], after=snapshot["after"],
        note=(
            f"reassigned to <@{slack_user_id}> ({new_actor_label})"
            + (f" — {note}" if note else "")
        ) if slack_user_id else note,
    )
    attribution = f" by <@{submitter_id}>" if submitter_id else ""
    target = f"<@{slack_user_id}>" if slack_user_id else new_actor_label
    return (
        f":arrows_counterclockwise: Reassigned{attribution} `VRB-{entity_id[:8]}` "
        f"to {target}."
    )


def _extract_input_value(
    values: dict[str, Any], block_id: str, action_id: str,
) -> str | None:
    """Pull a plain-text input value out of a Slack view-submission `state.values`."""
    block = values.get(block_id) or {}
    elem = block.get(action_id) or {}
    raw = elem.get("value")
    if raw is None:
        return None
    raw = str(raw).strip()
    return raw or None


def _extract_edit_values(values: dict[str, Any]) -> dict[str, str]:
    """Pull all `verbatim_edit_*` block inputs out of a view-submission state."""
    out: dict[str, str] = {}
    for block_id, fields in values.items():
        if not block_id.startswith("verbatim_edit_"):
            continue
        for action_id, elem in (fields or {}).items():
            raw = (elem or {}).get("value")
            if raw is None:
                continue
            raw = str(raw).strip()
            if raw:
                out[action_id] = raw
    # Strip out the audit-only note field — apply_edit takes it separately.
    out.pop("note", None)
    return out


def _resolve_slack_user_label(web_client, user_id: str | None) -> str | None:
    """Look up a Slack user's real_name / display_name via users.info."""
    if not user_id or web_client is None:
        return None
    try:
        resp = web_client.users_info(user=user_id)
        user = (resp.get("user") if hasattr(resp, "get") else resp.data.get("user")) or {}
        profile = user.get("profile") or {}
        return (
            profile.get("display_name_normalized")
            or profile.get("display_name")
            or profile.get("real_name_normalized")
            or profile.get("real_name")
            or user.get("name")
        )
    except Exception:  # noqa: BLE001
        log.exception("users.info lookup failed for %s", user_id)
        return None


def _merged_pill_text(merged_count: int) -> str:
    """Compact "+N merged" suffix shared by all Slack list views.

    Mirrors the web UI's `+N merged` pill so the same item reads the same
    way in both surfaces.
    """
    if not merged_count:
        return ""
    return f" _(+{merged_count} merged)_"


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
    if sub == "ask":
        if not parsed.args:
            return "_Usage: `/verbatim ask <question>`_"
        question = " ".join(parsed.args)
        try:
            result = ask.answer(conn, question)
        except Exception as e:  # noqa: BLE001
            log.exception("ask failed")
            return f"_Couldn't answer that — {e}_"
        return f":mag: *{_escape_mrkdwn(question)}*\n\n{result.answer}"
    if sub == "simplify":
        if not parsed.args:
            return "_Usage: `/verbatim simplify <id-prefix>`_"
        full_id = _resolve_id_prefix(conn, parsed.args[0])
        if full_id is None:
            return f"_No entity matches id prefix `{parsed.args[0]}`._"
        try:
            result = simplify.simplify_entity(conn, full_id)
        except Exception as e:  # noqa: BLE001
            log.exception("simplify failed")
            return f"_Couldn't simplify that — {e}_"
        if result is None:
            return f"_Entity not found: `{full_id}`._"
        return f":bulb: *Plain-language* `VRB-{full_id[:8]}`\n\n{result.text}"

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
        http_client: httpx.Client | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required (xoxb-... — set SLACK_BOT_TOKEN).")
        if not app_token and socket_client is None:
            raise ValueError("app_token is required for Socket Mode (xapp-... — set SLACK_APP_TOKEN).")
        self._web = web_client or WebClient(token=bot_token)
        self._socket = socket_client or SocketModeClient(app_token=app_token, web_client=self._web)
        self._http = http_client or httpx.Client(timeout=10.0)
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
            elif req.type == "interactive":
                payload = req.payload or {}
                if payload.get("type") == "view_submission":
                    self._handle_view_submission(payload)
                else:
                    self._handle_interactive(payload)
            # Other event types ignored for v1
        except Exception:  # noqa: BLE001
            log.exception("slack bot failed handling request")

    def _handle_interactive(self, payload: dict[str, Any]) -> None:
        """Handle block_actions interactions — button clicks on extraction cards."""
        if payload.get("type") != "block_actions":
            return
        actions = payload.get("actions") or []
        if not actions:
            return
        action = actions[0]
        action_id = action.get("action_id") or ""
        response_url = payload.get("response_url")
        user = (payload.get("user") or {}).get("id")
        trigger_id = payload.get("trigger_id")

        # Parse action_id format: "verbatim:<verb>:<entity_id>"
        try:
            _ns, verb, entity_id = action_id.split(":", 2)
        except ValueError:
            log.warning("unrecognized action_id: %r", action_id)
            return

        # Edit / Reassign open a Slack modal (views.open) for field-level editing.
        # The actual state change happens on view_submission via _handle_view_submission.
        if verb in ("edit", "reassign") and trigger_id:
            try:
                self._open_modal(verb=verb, entity_id=entity_id, trigger_id=trigger_id)
            except Exception:  # noqa: BLE001
                log.exception("failed opening %s modal for %s", verb, entity_id)
                # Fall through to text reply so the user knows the click was seen.
            else:
                return

        conn = state.open_db(self._db_path)
        try:
            reply = dispatch_action(conn, verb=verb, entity_id=entity_id, user_id=user)
        finally:
            conn.close()

        if response_url:
            try:
                self._post_response_url(response_url, reply)
                return
            except Exception:  # noqa: BLE001
                log.exception("failed posting interactive response_url")
        # Fallback: post in the channel the action came from
        channel = (payload.get("channel") or {}).get("id")
        if user and channel:
            try:
                self._web.chat_postEphemeral(channel=channel, user=user, text=reply)
            except Exception:  # noqa: BLE001
                log.exception("interactive ephemeral fallback failed")

    def _open_modal(
        self, *, verb: str, entity_id: str, trigger_id: str,
    ) -> None:
        """Open an edit or reassign modal via Slack views.open."""
        conn = state.open_db(self._db_path)
        try:
            entity = state.show_entity(conn, entity_id)
        finally:
            conn.close()
        if entity is None:
            raise ValueError(f"Entity not found: {entity_id}")
        if verb == "edit":
            view = build_edit_modal_view(entity)
        elif verb == "reassign":
            view = build_reassign_modal_view(entity)
        else:
            raise ValueError(f"Unsupported modal verb: {verb}")
        self._web.views_open(trigger_id=trigger_id, view=view)

    def _handle_view_submission(self, payload: dict[str, Any]) -> None:
        """Handle a Slack view_submission — applies edit / reassign + audit."""
        view = payload.get("view") or {}
        callback_id = view.get("callback_id") or ""
        submitter = (payload.get("user") or {}).get("id")

        try:
            _ns, verb, entity_id = callback_id.split(":", 2)
        except ValueError:
            log.warning("unrecognized view callback_id: %r", callback_id)
            return

        values = (view.get("state") or {}).get("values") or {}
        conn = state.open_db(self._db_path)
        try:
            if verb == "edit":
                submitted = _extract_edit_values(values)
                note = _extract_input_value(values, "verbatim_edit_note", "note")
                reply = apply_edit(
                    conn, entity_id=entity_id, submitted_values=submitted,
                    user_id=submitter, user_label=None, note=note,
                )
            elif verb == "reassign":
                user_block = values.get("verbatim_reassign_user") or {}
                slack_user_id = (user_block.get("user") or {}).get("selected_user")
                override_name = _extract_input_value(
                    values, "verbatim_reassign_name", "name",
                )
                note = _extract_input_value(values, "verbatim_reassign_note", "note")
                label = override_name or _resolve_slack_user_label(
                    self._web, slack_user_id,
                )
                reply = apply_reassign(
                    conn, entity_id=entity_id,
                    new_actor_label=label or "—",
                    slack_user_id=slack_user_id, submitter_id=submitter,
                    note=note,
                )
            else:
                reply = f"_Unknown view callback `{verb}`._"
        finally:
            conn.close()

        # views don't carry a response_url; ephemeral the result back to the user
        # in their DM with the bot if we have channel context, otherwise log.
        if submitter:
            try:
                self._try_dm_user(submitter, reply)
            except Exception:  # noqa: BLE001
                log.exception("view-submission DM fallback failed")

    def _handle_slash_command(self, payload: dict[str, Any]) -> None:
        text = payload.get("text") or ""
        response_url = payload.get("response_url")
        channel_id = payload.get("channel_id")
        user_id = payload.get("user_id")
        if not response_url and not channel_id:
            log.warning("slash command had no response_url or channel_id: %r", payload)
            return

        parsed = parse_command_text(text)
        conn = state.open_db(self._db_path)
        try:
            body = dispatch_command(parsed, conn)
        finally:
            conn.close()

        # Prefer response_url — Slack provides this specifically so bots can
        # reply ephemerally to slash commands without needing channel membership.
        # This avoids the `not_in_channel` error users hit when they run
        # /verbatim in a channel the bot hasn't been invited to.
        if response_url:
            try:
                self._post_response_url(response_url, body)
                return
            except Exception:  # noqa: BLE001
                log.exception("failed posting to response_url; falling back to Web API")

        # Fallbacks if response_url is missing or failed.
        if user_id and channel_id:
            try:
                self._web.chat_postEphemeral(channel=channel_id, user=user_id, text=body)
                return
            except Exception:  # noqa: BLE001
                log.exception("chat.postEphemeral failed; trying open-DM fallback")
                if self._try_dm_user(user_id, body):
                    return
        if channel_id:
            try:
                self._web.chat_postMessage(channel=channel_id, text=body)
            except Exception:  # noqa: BLE001
                log.exception("chat.postMessage failed too; giving up on this command")

    def _post_response_url(self, url: str, text: str) -> None:
        """POST a slash-command reply via Slack's per-invocation response_url."""
        resp = self._http.post(
            url,
            json={"response_type": "ephemeral", "text": text},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()

    def _try_dm_user(self, user_id: str, text: str) -> bool:
        """Open a DM with the user and send the message. Returns True if sent."""
        try:
            conv = self._web.conversations_open(users=user_id)
            dm_channel = (conv.get("channel") or {}).get("id")
            if not dm_channel:
                return False
            self._web.chat_postMessage(channel=dm_channel, text=text)
            return True
        except Exception:  # noqa: BLE001
            log.exception("DM fallback failed")
            return False

    # ----- run loop -----

    def run(self) -> None:
        """Connect to Slack and block forever."""
        self._socket.socket_mode_request_listeners.append(self._on_request)
        self._socket.connect()
        # Block by waiting on the socket. Use an Event the operator can SIGINT.
        import threading
        threading.Event().wait()

    # ----- outbound: extraction card with interactive buttons -----

    def post_extraction_card(self, channel: str, entity_id: str) -> dict[str, Any]:
        """Post an interactive extraction card for one entity to a channel.

        The card shows the entity summary + verbatim quote + buttons:
        Confirm, Dismiss, Open in Verbatim. Button clicks fire block_actions
        events that the bot handles (see `_handle_interactive`).
        """
        conn = state.open_db(self._db_path)
        try:
            entity = state.show_entity(conn, entity_id)
        finally:
            conn.close()
        if entity is None:
            raise ValueError(f"Entity not found: {entity_id}")
        blocks = build_extraction_card_blocks(entity)
        fallback = f"Verbatim {entity['kind']}: {_result_summary(entity)}"
        return self._web.chat_postMessage(
            channel=channel, text=fallback, blocks=blocks,
        ).data  # type: ignore[no-any-return]

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

    # ----- outbound: deadline nudge -----

    def post_nudge(self, channel: str, *, within_days: int = 7) -> dict[str, Any]:
        """Post a deadline nudge — overdue + due-soon commitments — to a channel."""
        conn = state.open_db(self._db_path)
        try:
            body = self._render_nudge(conn, within_days=within_days)
        finally:
            conn.close()
        return self._web.chat_postMessage(channel=channel, text=body).data  # type: ignore[no-any-return]

    def _render_nudge(self, conn: sqlite3.Connection, *, within_days: int = 7) -> str:
        overdue = state.overdue_commitments(conn)
        due_soon = state.due_soon_commitments(conn, within_days=within_days)

        if not overdue and not due_soon:
            return (
                ":white_check_mark: *Verbatim deadline check* — "
                "nothing overdue, nothing due in the next "
                f"{within_days} days. All clear."
            )

        sections = ["*:alarm_clock: Verbatim deadline nudge*", ""]
        if overdue:
            sections.append(f"*:rotating_light: Overdue ({len(overdue)}):*")
            for it in overdue:
                p = it["payload"]
                days = abs(it.get("days_until") or 0)
                actor = _escape_mrkdwn(p.get("actor") or "?")
                what = _escape_mrkdwn(p.get("deliverable") or "?")
                sections.append(f"• *{actor}* — {what}  _({days}d overdue)_")
            sections.append("")
        if due_soon:
            sections.append(f"*:hourglass_flowing_sand: Due soon ({len(due_soon)}):*")
            for it in due_soon:
                p = it["payload"]
                days = it.get("days_until")
                actor = _escape_mrkdwn(p.get("actor") or "?")
                what = _escape_mrkdwn(p.get("deliverable") or "?")
                when = "today" if days == 0 else f"in {days}d"
                sections.append(f"• *{actor}* — {what}  _({when})_")
        return "\n".join(sections).rstrip()
