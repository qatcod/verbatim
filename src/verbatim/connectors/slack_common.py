"""Slack ingestion primitives shared by the export and Web API connectors.

The two connectors differ only in *where the messages come from* — a workspace
export ZIP, or a live Slack Web API call. From the messages onward, thread
reconstruction, transcript rendering, user-ID resolution, and noise filtering
are the same. That logic lives here so neither connector duplicates it.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Slack message subtypes that aren't real content — bot churn, joins, leaves.
# Conservative on purpose: better to ingest a slightly-low-signal subtype than
# silently lose meaningful messages.
NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "pinned_item",
        "unpinned_item",
        "bot_add",
        "bot_remove",
        "reminder_add",
    }
)


@dataclass
class SlackMessage:
    ts: str  # original Slack timestamp string ("1705344000.000100")
    user_id: str | None
    text: str
    thread_ts: str | None = None  # if part of a thread, the parent's ts
    subtype: str | None = None
    reply_count: int = 0

    @property
    def datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), tz=timezone.utc)


@dataclass
class SlackUnit:
    """One extraction-ready unit — a thread, or a day-rollup of loose messages."""

    kind: str  # "thread" or "channel_day"
    channel: str
    start: datetime
    end: datetime
    messages: list[SlackMessage]
    user_map: dict[str, str] = field(default_factory=dict)  # user_id -> display name
    title: str = ""

    @property
    def transcript(self) -> str:
        return render_transcript(self)

    @property
    def source_kind(self) -> str:
        return "slack_thread" if self.kind == "thread" else "slack_channel_day"

    @property
    def source_label(self) -> str:
        """Short human-readable identifier — used as source_path in the state DB."""
        if self.kind == "thread":
            return f"slack://#{self.channel}/thread/{self.start.strftime('%Y-%m-%dT%H:%M')}"
        return f"slack://#{self.channel}/day/{self.start.date().isoformat()}"


# ----------------------- message parsing -----------------------


def parse_message(raw: dict[str, Any]) -> SlackMessage | None:
    """Convert a raw Slack message dict (from export or API) to SlackMessage.

    Returns None for non-message types and noise subtypes.
    """
    if raw.get("type") != "message":
        return None
    subtype = raw.get("subtype")
    if subtype in NOISE_SUBTYPES:
        return None
    ts = raw.get("ts")
    if not ts:
        return None
    return SlackMessage(
        ts=str(ts),
        user_id=raw.get("user") or raw.get("bot_id"),
        text=(raw.get("text") or "").strip(),
        thread_ts=str(raw["thread_ts"]) if raw.get("thread_ts") else None,
        subtype=subtype,
        reply_count=int(raw.get("reply_count", 0) or 0),
    )


def build_user_map(data: Any) -> dict[str, str]:
    """Pick the best display name for each user from a list of user dicts.

    Handles both export users.json shape and Web API users.list response shape
    (both are arrays of user objects with the same field layout).
    """
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for u in data:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        if not uid:
            continue
        profile = u.get("profile") or {}
        name = (
            profile.get("display_name_normalized")
            or profile.get("display_name")
            or profile.get("real_name_normalized")
            or profile.get("real_name")
            or u.get("name")
            or uid
        )
        out[uid] = name
    return out


# ----------------------- unit building -----------------------


def build_units_from_messages(
    *,
    channel: str,
    messages: list[SlackMessage],
    user_map: dict[str, str],
    min_thread_messages: int = 3,
    include_loose_messages: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[SlackUnit]:
    """Turn a flat list of messages for one channel into extraction-ready units.

    - threads with >= min_thread_messages messages → one SlackUnit each
    - if include_loose_messages: non-threaded messages bucketed by UTC date,
      one SlackUnit per bucket with >= 2 messages
    """
    messages = [m for m in messages if _within(m, since, until)]
    if not messages:
        return

    yield from _build_threads(channel, messages, user_map, min_thread_messages)
    if include_loose_messages:
        yield from _build_channel_day_rollups(channel, messages, user_map)


def _build_threads(
    channel: str,
    messages: list[SlackMessage],
    user_map: dict[str, str],
    min_messages: int,
) -> Iterator[SlackUnit]:
    threads: dict[str, list[SlackMessage]] = {}
    for m in messages:
        if m.thread_ts:
            threads.setdefault(m.thread_ts, []).append(m)
    for msgs in threads.values():
        if len(msgs) < min_messages:
            continue
        msgs.sort(key=lambda m: float(m.ts))
        yield SlackUnit(
            kind="thread",
            channel=channel,
            start=msgs[0].datetime_utc,
            end=msgs[-1].datetime_utc,
            messages=msgs,
            user_map=user_map,
            title=_first_line(msgs[0].text or "(no parent text)"),
        )


def _build_channel_day_rollups(
    channel: str,
    messages: list[SlackMessage],
    user_map: dict[str, str],
) -> Iterator[SlackUnit]:
    loose = [m for m in messages if m.thread_ts is None]
    if not loose:
        return
    buckets: dict[str, list[SlackMessage]] = {}
    for m in loose:
        day = m.datetime_utc.date().isoformat()
        buckets.setdefault(day, []).append(m)
    for day, msgs in sorted(buckets.items()):
        if len(msgs) < 2:
            continue
        msgs.sort(key=lambda m: float(m.ts))
        yield SlackUnit(
            kind="channel_day",
            channel=channel,
            start=msgs[0].datetime_utc,
            end=msgs[-1].datetime_utc,
            messages=msgs,
            user_map=user_map,
            title=f"#{channel} on {day}",
        )


# ----------------------- transcript rendering -----------------------


def render_transcript(unit: SlackUnit) -> str:
    """Render a SlackUnit as a transcript the extractor can consume.

    Format:
        Channel: #general
        Thread started 2024-01-15 10:30 UTC

        [10:30] @alice: should we use postgres or sqlite?
        [10:32] @bob: postgres for v1, easier to migrate
    """
    header_lines = [f"Channel: #{unit.channel}"]
    if unit.kind == "thread":
        header_lines.append(f"Thread started {unit.start.strftime('%Y-%m-%d %H:%M UTC')}")
    else:
        header_lines.append(f"Channel rollup {unit.start.date().isoformat()}")
    header_lines.append("")
    return "\n".join(header_lines + _format_messages(unit.messages, unit.user_map)) + "\n"


def _format_messages(messages: list[SlackMessage], users: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for m in messages:
        ts = m.datetime_utc.strftime("%H:%M")
        author = users.get(m.user_id or "", m.user_id or "<unknown>")
        text = replace_user_mentions(m.text, users)
        lines.append(f"[{ts}] @{author}: {text}")
    return lines


def replace_user_mentions(text: str, users: dict[str, str]) -> str:
    """Replace <@U12345> with @display-name where the map knows it."""
    if not users:
        return text
    out = text
    for uid, name in users.items():
        out = out.replace(f"<@{uid}>", f"@{name}")
    return out


# ----------------------- helpers -----------------------


def _within(m: SlackMessage, since: datetime | None, until: datetime | None) -> bool:
    if since and m.datetime_utc < since:
        return False
    if until and m.datetime_utc > until:
        return False
    return True


def _first_line(text: str, *, max_chars: int = 80) -> str:
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= max_chars else line[:max_chars] + "…"
