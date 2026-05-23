"""Slack Web API connector — pull live messages from a workspace.

The export connector handles one-time historical backfill from a downloaded ZIP.
This connector pulls live, repeatedly, from a Slack App's OAuth token. The
output is identical (SlackUnit objects); only the source changes.

# Setting up a Slack App

You need a token (a bot token, `xoxb-...`, or a user token, `xoxp-...`):

1. https://api.slack.com/apps → Create New App → From scratch
2. Pick a name and the workspace
3. OAuth & Permissions → add Bot Token Scopes:
     channels:history     (read messages in public channels the bot's in)
     channels:read        (list public channels)
     users:read           (resolve user IDs to names)
   Optional:
     groups:history       (private channels the bot is in)
     im:history           (DMs to the bot)
     mpim:history         (group DMs the bot is in)
4. Install the App to your workspace
5. Copy the "Bot User OAuth Token" — that's the value to set as
   $SLACK_TOKEN or pass via --token.
6. **Invite the bot into every channel you want to read history from:**
     /invite @YourBotName
   This is the part Bot tokens can't skip — `conversations.history` requires
   the bot to be a channel member. There's no API-level workaround.

# Bot token vs User token

If inviting the bot to every channel is friction, use a **User token**
(`xoxp-...`) instead. User tokens act as the human and can read any channel
that human can already read — no invites needed.

   - OAuth & Permissions → User Token Scopes:
       channels:history, groups:history, im:history, mpim:history, users:read
   - Install/Reinstall the App
   - Copy the "User OAuth Token" instead.

# Rate limits

`conversations.history` and `conversations.replies` are tier-3 endpoints — Slack
allows ~50 calls per minute per workspace. The slack_sdk client raises
SlackApiError on 429; we let it bubble up and surface the cause to the CLI.
"""
from __future__ import annotations

import http.client
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .slack_common import (
    SlackMessage,
    SlackUnit,
    build_units_from_messages,
    build_user_map,
    parse_message,
)

# Transport errors that show up on large workspaces — the response body claims a
# content-length the server doesn't fully deliver. Retrying with a smaller page
# size almost always succeeds.
_TRANSIENT_TRANSPORT = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    TimeoutError,
)
# Page sizes for paginated `*.list` calls. Slack's docs allow 1000 but huge
# pages routinely truncate against ~1000-channel workspaces. 100 keeps the
# response bounded; we'll back off to 25 on transport failure.
_LIST_PAGE = 100
_LIST_PAGE_SMALL = 25
_LIST_RETRIES = 3

# Slack API error codes we treat as "this channel is inaccessible, skip it"
# rather than "the whole run is broken".
_PER_CHANNEL_ERRORS = frozenset({
    "not_in_channel",
    "channel_not_found",
    "is_archived",
    "missing_scope",  # bot lacks a scope for this channel type (private/im/mpim)
})


class ChannelNotAccessible(Exception):
    """Raised when the token can't read a specific channel's history.

    Carries the channel name plus a hint about the specific Slack error and
    how to remedy it. The connector raises this in place of SlackApiError
    so callers can catch one stable type and decide whether to skip or fail.
    """

    def __init__(self, channel: str, slack_error: str, hint: str) -> None:
        self.channel = channel
        self.slack_error = slack_error
        self.hint = hint
        super().__init__(f"#{channel}: {slack_error} — {hint}")


class SlackClient:
    """A thin wrapper around slack_sdk.WebClient that yields SlackUnits.

    Stateless except for an in-process user cache. Safe to construct fresh
    per CLI invocation.
    """

    def __init__(self, token: str, *, request_pause: float = 0.0) -> None:
        if not token:
            raise ValueError("Slack token is required (set SLACK_TOKEN or pass --token).")
        self._client = WebClient(token=token)
        self._users_cache: dict[str, str] | None = None
        self._request_pause = request_pause

    # ----- discovery -----

    def _paginated_list(
        self,
        api_call: Callable[..., Any],
        items_key: str,
        *,
        base_params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items from any paginated Slack `*.list` endpoint.

        Handles cursor pagination and retries transient transport failures
        (Slack truncating responses on 1000+ channel workspaces) with a
        smaller page size on the retry. Yields one item dict at a time so
        callers can stop early when they find what they need.
        """
        cursor: str | None = None
        page = _LIST_PAGE
        while True:
            params = dict(base_params or {})
            params["limit"] = page
            if cursor:
                params["cursor"] = cursor
            resp = None
            for attempt in range(_LIST_RETRIES):
                try:
                    resp = api_call(**params)
                    break
                except _TRANSIENT_TRANSPORT:
                    # Half the page size on each retry to fit under the
                    # truncation threshold.
                    page = max(_LIST_PAGE_SMALL, page // 2)
                    params["limit"] = page
                    if attempt == _LIST_RETRIES - 1:
                        raise
                    time.sleep(1.0)
                except SlackApiError as e:
                    msg = str(e).lower()
                    if "incomplete" in msg or "connection" in msg:
                        page = max(_LIST_PAGE_SMALL, page // 2)
                        params["limit"] = page
                        if attempt == _LIST_RETRIES - 1:
                            raise
                        time.sleep(1.0)
                        continue
                    raise
            assert resp is not None
            yield from (resp.get(items_key) or [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return
            self._maybe_pause()

    def get_users(self) -> dict[str, str]:
        """Fetch and cache user_id → display name for the workspace."""
        if self._users_cache is not None:
            return self._users_cache
        users_raw = list(self._paginated_list(self._client.users_list, "members"))
        self._users_cache = build_user_map(users_raw)
        return self._users_cache

    def list_channel_names(self, *, include_private: bool = False) -> list[str]:
        """All channels the token has access to (public; private if requested)."""
        types = "public_channel" + (",private_channel" if include_private else "")
        base = {"types": types, "exclude_archived": True}
        names: list[str] = []
        for ch in self._paginated_list(
            self._client.conversations_list, "channels", base_params=base,
        ):
            name = ch.get("name")
            if name:
                names.append(name)
        return sorted(names)

    def _resolve_channel_id(self, name: str, *, include_private: bool = False) -> str | None:
        """Map a channel name to a channel ID (Slack API needs IDs, not names)."""
        types = "public_channel" + (",private_channel" if include_private else "")
        base = {"types": types, "exclude_archived": True}
        for ch in self._paginated_list(
            self._client.conversations_list, "channels", base_params=base,
        ):
            if ch.get("name") == name:
                return ch.get("id")
        return None

    # ----- message fetching -----

    def iter_messages(
        self,
        channel_name: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_private: bool = False,
    ) -> Iterator[SlackMessage]:
        """Yield messages from a channel, including thread replies, in time order.

        Pulls top-level history first, then expands threads via conversations.replies.

        Raises `ChannelNotAccessible` if the channel exists in the workspace but
        the token can't read its history (most often: bot is not a member).
        """
        channel_id = self._resolve_channel_id(channel_name, include_private=include_private)
        if channel_id is None:
            raise ChannelNotAccessible(
                channel=channel_name,
                slack_error="channel_not_found",
                hint=(
                    f"#{channel_name} isn't visible to this token. "
                    f"For private channels, the bot needs `groups:read` and must be "
                    f"a member, or use a User token (xoxp-...) that already sees it."
                ),
            )

        oldest = f"{since.timestamp():.6f}" if since else "0"
        latest = f"{until.timestamp():.6f}" if until else None

        # Collect top-level messages with pagination.
        top_level: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "channel": channel_id,
                "oldest": oldest,
                "limit": 200,
            }
            if latest:
                params["latest"] = latest
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self._client.conversations_history(**params)
            except SlackApiError as e:
                err = (e.response or {}).get("error", "")
                if err in _PER_CHANNEL_ERRORS:
                    raise ChannelNotAccessible(
                        channel=channel_name,
                        slack_error=err,
                        hint=_hint_for_error(err, channel_name),
                    ) from e
                raise
            top_level.extend(resp.get("messages") or [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
            self._maybe_pause()

        # Expand threads: for any parent with replies, fetch them.
        for raw in top_level:
            parsed = parse_message(raw)
            if parsed is not None:
                yield parsed
            reply_count = int(raw.get("reply_count", 0) or 0)
            thread_ts = raw.get("thread_ts")
            ts = raw.get("ts")
            # A thread parent has thread_ts == ts. Replies have reply_count==0 and
            # thread_ts!=ts. We only need to fetch replies for parents (avoids dupes).
            if reply_count > 0 and thread_ts == ts:
                yield from self._iter_thread_replies(channel_id, str(ts))

    def _iter_thread_replies(self, channel_id: str, parent_ts: str) -> Iterator[SlackMessage]:
        cursor: str | None = None
        first_call = True
        while True:
            params: dict[str, Any] = {"channel": channel_id, "ts": parent_ts, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._client.conversations_replies(**params)
            for raw in resp.get("messages") or []:
                # The first message in the replies response is the parent — skip on
                # the first call so we don't double-yield it (we already yielded
                # it from conversations.history).
                if first_call and str(raw.get("ts")) == parent_ts:
                    continue
                parsed = parse_message(raw)
                if parsed is not None:
                    yield parsed
            first_call = False
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
            self._maybe_pause()

    # ----- unit-level convenience -----

    def iter_units(
        self,
        *,
        channels: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_thread_messages: int = 3,
        include_loose_messages: bool = False,
        include_private: bool = False,
        on_channel_error: Callable[[ChannelNotAccessible], None] | None = None,
    ) -> Iterator[SlackUnit]:
        """Live equivalent of SlackExport.iter_units. Pulls user map once, then
        walks each requested channel and emits SlackUnit objects from the same
        shared builder used by the export connector.

        `on_channel_error`: callback fired when a channel can't be read (bot not
        a member, missing scope, archived, etc.). The channel is skipped and
        the run continues with the rest. If the callback is None, the error
        is re-raised — preserving prior fail-fast behaviour for callers that
        prefer it.
        """
        users = self.get_users()
        chans = channels if channels else self.list_channel_names(include_private=include_private)
        for channel in chans:
            try:
                messages = list(
                    self.iter_messages(
                        channel,
                        since=since,
                        until=until,
                        include_private=include_private,
                    )
                )
            except ChannelNotAccessible as e:
                if on_channel_error is None:
                    raise
                on_channel_error(e)
                continue
            if not messages:
                continue
            yield from build_units_from_messages(
                channel=channel,
                messages=messages,
                user_map=users,
                min_thread_messages=min_thread_messages,
                include_loose_messages=include_loose_messages,
                since=since,
                until=until,
            )

    # ----- internals -----

    def _maybe_pause(self) -> None:
        if self._request_pause > 0:
            time.sleep(self._request_pause)


def _hint_for_error(slack_error: str, channel: str) -> str:
    """Map a Slack API error to an actionable hint."""
    if slack_error == "not_in_channel":
        return (
            f"the bot is not a member of #{channel}. Invite it with "
            f"`/invite @<your-bot-name>` in the channel, or use a User token "
            f"(xoxp-...) instead of a Bot token — User tokens act as you and "
            f"don't need invites."
        )
    if slack_error == "channel_not_found":
        return (
            f"#{channel} doesn't exist or isn't visible to this token. "
            f"Check spelling, or grant the right scope (groups:read for "
            f"private channels)."
        )
    if slack_error == "is_archived":
        return f"#{channel} is archived. Slack won't return its history."
    if slack_error == "missing_scope":
        return (
            f"the token lacks the right scope to read #{channel} "
            f"(channels:history / groups:history / im:history / mpim:history "
            f"depending on the channel type). Add it in OAuth & Permissions, "
            f"reinstall the App."
        )
    return "this channel is not readable with the current token."
