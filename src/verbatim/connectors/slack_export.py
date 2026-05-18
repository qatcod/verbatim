"""Slack workspace export parser.

Reads the offline ZIP (or pre-extracted directory) you get from
Slack admin → Settings → Import/Export Data → Export.

Output: an iterable of `SlackUnit` records — one per thread, or one per
"loose messages in a channel on a date" rollup — each with a transcript-shaped
text body the extractor can ingest as-is.

Why thread-level granularity: a Slack thread is usually a self-contained
discussion (the analog of a meeting), and treating each thread as its own
extraction yields cleaner state than dumping a whole channel's noise into
one giant request. Channels with high signal but no threads (rare) can be
captured via the include_loose_messages option.

Auth/scopes: none. The export is a static file the customer already has.
This is the deliberate v1.0 wedge — zero customer setup beyond producing
the ZIP they can already produce.
"""
from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Slack message subtypes that aren't real content — bot churn, joins, leaves.
# Keep this conservative; better to ingest a slightly-low-signal subtype than
# silently lose meaningful messages.
_NOISE_SUBTYPES = frozenset(
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
        return _render_transcript(self)

    @property
    def source_kind(self) -> str:
        return "slack_thread" if self.kind == "thread" else "slack_channel_day"

    @property
    def source_label(self) -> str:
        """Short human-readable identifier — used as source_path in the state DB."""
        if self.kind == "thread":
            return f"slack://#{self.channel}/thread/{self.start.strftime('%Y-%m-%dT%H:%M')}"
        return f"slack://#{self.channel}/day/{self.start.date().isoformat()}"


@dataclass
class SlackExport:
    """A loaded Slack workspace export."""

    path: Path
    users: dict[str, str] = field(default_factory=dict)  # user_id -> display name
    channels: list[str] = field(default_factory=list)
    _zip: zipfile.ZipFile | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> SlackExport:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- discovery -----

    def iter_units(
        self,
        *,
        channels: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_thread_messages: int = 3,
        include_loose_messages: bool = False,
    ) -> Iterator[SlackUnit]:
        """Yield extraction-ready units from the export.

        Filters:
        - channels: limit to these channel names (None = all)
        - since / until: date window (UTC) to keep
        - min_thread_messages: skip threads shorter than this
        - include_loose_messages: also emit channel-day rollups of non-threaded
            messages, useful for channels that use few threads. Off by default
            because the signal/noise tends to be poor.
        """
        wanted = set(channels) if channels else None
        for channel in self.channels:
            if wanted is not None and channel not in wanted:
                continue
            messages = list(self._iter_channel_messages(channel))
            if not messages:
                continue
            messages = [m for m in messages if _within(m, since, until)]
            if not messages:
                continue

            yield from self._build_threads(channel, messages, min_thread_messages)

            if include_loose_messages:
                yield from self._build_channel_day_rollups(channel, messages)

    # ----- internals -----

    def _build_threads(
        self,
        channel: str,
        messages: list[SlackMessage],
        min_messages: int,
    ) -> Iterator[SlackUnit]:
        # group messages by thread_ts.
        threads: dict[str, list[SlackMessage]] = {}
        for m in messages:
            if m.thread_ts:
                threads.setdefault(m.thread_ts, []).append(m)
        for _parent_ts, msgs in threads.items():
            if len(msgs) < min_messages:
                continue
            msgs.sort(key=lambda m: float(m.ts))
            yield SlackUnit(
                kind="thread",
                channel=channel,
                start=msgs[0].datetime_utc,
                end=msgs[-1].datetime_utc,
                messages=msgs,
                user_map=self.users,
                title=_first_line(msgs[0].text or "(no parent text)"),
            )

    def _build_channel_day_rollups(
        self,
        channel: str,
        messages: list[SlackMessage],
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
                user_map=self.users,
                title=f"#{channel} on {day}",
            )

    def _iter_channel_messages(self, channel: str) -> Iterator[SlackMessage]:
        for raw_path in self._list_channel_files(channel):
            payload = self._read_json(raw_path)
            if not isinstance(payload, list):
                continue
            for raw in payload:
                msg = _parse_message(raw)
                if msg is None:
                    continue
                yield msg

    def _list_channel_files(self, channel: str) -> list[str]:
        """List daily JSON files for one channel, sorted."""
        if self._zip is not None:
            prefix = f"{channel}/"
            names = [n for n in self._zip.namelist() if n.startswith(prefix) and n.endswith(".json")]
        else:
            base = self.path / channel
            if not base.is_dir():
                return []
            names = [str(p.relative_to(self.path)) for p in base.glob("*.json")]
        return sorted(names)

    def _read_json(self, name: str) -> Any:
        if self._zip is not None:
            with self._zip.open(name) as f:
                return json.load(f)
        return json.loads((self.path / name).read_text(encoding="utf-8"))


# ----- module-level helpers -----


def load(path: str | Path) -> SlackExport:
    """Open a Slack export from a ZIP file or an already-extracted directory."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Slack export not found: {p}")

    export = SlackExport(path=p)
    if p.is_file() and p.suffix.lower() == ".zip":
        export._zip = zipfile.ZipFile(p, "r")
        export.users = _load_users_from_zip(export._zip)
        export.channels = _list_channels_from_zip(export._zip)
    elif p.is_dir():
        export.users = _load_users_from_dir(p)
        export.channels = _list_channels_from_dir(p)
    else:
        raise ValueError(f"Expected .zip file or directory, got: {p}")
    return export


def _parse_message(raw: dict[str, Any]) -> SlackMessage | None:
    if raw.get("type") != "message":
        return None
    subtype = raw.get("subtype")
    if subtype in _NOISE_SUBTYPES:
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


def _load_users_from_zip(z: zipfile.ZipFile) -> dict[str, str]:
    try:
        with z.open("users.json") as f:
            data = json.load(f)
    except KeyError:
        return {}
    return _build_user_map(data)


def _load_users_from_dir(d: Path) -> dict[str, str]:
    p = d / "users.json"
    if not p.exists():
        return {}
    return _build_user_map(json.loads(p.read_text(encoding="utf-8")))


def _build_user_map(data: Any) -> dict[str, str]:
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


def _list_channels_from_zip(z: zipfile.ZipFile) -> list[str]:
    names = z.namelist()
    channels: set[str] = set()
    for n in names:
        if n.endswith(".json") and "/" in n and not n.endswith("/channels.json") and not n.endswith("/users.json"):
            channel = n.split("/", 1)[0]
            if channel not in {"users.json", "channels.json", "groups.json", "integration_logs.json"}:
                channels.add(channel)
    return sorted(channels)


def _list_channels_from_dir(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def _within(m: SlackMessage, since: datetime | None, until: datetime | None) -> bool:
    if since and m.datetime_utc < since:
        return False
    if until and m.datetime_utc > until:
        return False
    return True


def _first_line(text: str, *, max_chars: int = 80) -> str:
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= max_chars else line[:max_chars] + "…"


def _render_transcript(unit: SlackUnit) -> str:
    """Render a SlackUnit as a transcript the extractor can consume.

    Format:
        Channel: #general
        Thread started 2024-01-15 10:30 UTC

        [10:30] @qat: should we use postgres or sqlite?
        [10:32] @jason: postgres for v1, easier to migrate
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
        text = _replace_user_mentions(m.text, users)
        lines.append(f"[{ts}] @{author}: {text}")
    return lines


def _replace_user_mentions(text: str, users: dict[str, str]) -> str:
    """Replace <@U12345> with @display-name where the map knows it."""
    if not users:
        return text
    out = text
    for uid, name in users.items():
        out = out.replace(f"<@{uid}>", f"@{name}")
    return out
