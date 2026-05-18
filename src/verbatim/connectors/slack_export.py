"""Slack workspace export parser.

Reads the offline ZIP (or pre-extracted directory) you get from
Slack admin → Settings → Import/Export Data → Export.

Use this for one-time historical backfill. For continuous ingestion of a live
workspace, see `slack_api.py` (the Web API connector — same output, but
pulls live via OAuth token instead of from a downloaded archive).

Auth/scopes: none for this connector. The export is a static file the customer
already has.
"""
from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Re-export the shared types and helpers so existing callers (tests, CLI)
# can import everything they need from one module.
from .slack_common import (
    NOISE_SUBTYPES as _NOISE_SUBTYPES,  # noqa: F401  (kept for backward-compat)
)
from .slack_common import (
    SlackMessage,  # noqa: F401
    SlackUnit,
    _first_line,  # noqa: F401  (re-exported for tests)
    build_units_from_messages,
)
from .slack_common import (
    build_user_map as _build_user_map,  # noqa: F401
)
from .slack_common import (
    parse_message as _parse_message,  # noqa: F401
)
from .slack_common import (
    replace_user_mentions as _replace_user_mentions,  # noqa: F401
)


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

    def iter_units(
        self,
        *,
        channels: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_thread_messages: int = 3,
        include_loose_messages: bool = False,
    ) -> Iterator[SlackUnit]:
        wanted = set(channels) if channels else None
        for channel in self.channels:
            if wanted is not None and channel not in wanted:
                continue
            messages = list(self._iter_channel_messages(channel))
            if not messages:
                continue
            yield from build_units_from_messages(
                channel=channel,
                messages=messages,
                user_map=self.users,
                min_thread_messages=min_thread_messages,
                include_loose_messages=include_loose_messages,
                since=since,
                until=until,
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


def _list_channels_from_zip(z: zipfile.ZipFile) -> list[str]:
    names = z.namelist()
    channels: set[str] = set()
    for n in names:
        if not n.endswith(".json") or "/" not in n:
            continue
        channel = n.split("/", 1)[0]
        if channel in {"users.json", "channels.json", "groups.json", "integration_logs.json"}:
            continue
        channels.add(channel)
    return sorted(channels)


def _list_channels_from_dir(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.is_dir())
