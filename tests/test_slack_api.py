"""Slack Web API connector tests — all responses mocked, no network."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from verbatim.connectors import slack_api


class FakeWebClient:
    """A drop-in stand-in for slack_sdk.WebClient with canned responses.

    Records every call into `self.calls` for assertions.
    """

    def __init__(
        self,
        *,
        users: list[dict[str, Any]] | None = None,
        channels: list[dict[str, Any]] | None = None,
        history_by_channel: dict[str, list[dict[str, Any]]] | None = None,
        replies_by_parent: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.users = users or []
        self.channels = channels or []
        self.history_by_channel = history_by_channel or {}
        self.replies_by_parent = replies_by_parent or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def users_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("users_list", kwargs))
        return {"members": self.users, "response_metadata": {"next_cursor": ""}}

    def conversations_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations_list", kwargs))
        return {"channels": self.channels, "response_metadata": {"next_cursor": ""}}

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        chan = kwargs["channel"]
        self.calls.append(("conversations_history", kwargs))
        return {
            "messages": self.history_by_channel.get(chan, []),
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        parent_ts = kwargs["ts"]
        self.calls.append(("conversations_replies", kwargs))
        return {
            "messages": self.replies_by_parent.get(parent_ts, []),
            "response_metadata": {"next_cursor": ""},
        }


def make_client(**fake_kwargs: Any) -> tuple[slack_api.SlackClient, FakeWebClient]:
    """Construct a SlackClient with a token, then wire in a FakeWebClient."""
    client = slack_api.SlackClient(token="xoxb-test-token")
    fake = FakeWebClient(**fake_kwargs)
    client._client = fake  # type: ignore[assignment]
    return client, fake


# ----- constructor -----


def test_constructor_requires_token() -> None:
    with pytest.raises(ValueError):
        slack_api.SlackClient(token="")


# ----- users -----


def test_get_users_caches_and_maps_names() -> None:
    client, fake = make_client(
        users=[
            {"id": "U001", "profile": {"display_name_normalized": "qat"}},
            {"id": "U002", "profile": {"real_name_normalized": "Jason Remillard"}},
        ]
    )
    users = client.get_users()
    assert users == {"U001": "qat", "U002": "Jason Remillard"}

    # second call must NOT hit the API (cache)
    users2 = client.get_users()
    assert users2 is users
    user_calls = [c for c in fake.calls if c[0] == "users_list"]
    assert len(user_calls) == 1


# ----- channel discovery -----


def test_list_channel_names_sorted() -> None:
    client, _ = make_client(
        channels=[
            {"id": "C001", "name": "general"},
            {"id": "C002", "name": "engineering"},
            {"id": "C003", "name": "random"},
        ]
    )
    names = client.list_channel_names()
    assert names == ["engineering", "general", "random"]


def test_resolve_channel_id_finds_match() -> None:
    client, _ = make_client(
        channels=[
            {"id": "C001", "name": "general"},
            {"id": "C002", "name": "engineering"},
        ]
    )
    assert client._resolve_channel_id("engineering") == "C002"
    assert client._resolve_channel_id("nonexistent") is None


# ----- message fetching -----


def test_iter_messages_yields_top_level_and_thread_replies() -> None:
    parent_ts = "1700000000.000100"
    client, fake = make_client(
        channels=[{"id": "C001", "name": "engineering"}],
        history_by_channel={
            "C001": [
                {
                    "type": "message", "user": "U001", "text": "should we use sqlite?",
                    "ts": parent_ts, "thread_ts": parent_ts, "reply_count": 2,
                },
                {
                    "type": "message", "user": "U002", "text": "lunch?",
                    "ts": "1700000500.000200",
                },
            ]
        },
        replies_by_parent={
            parent_ts: [
                # API returns the parent again as the first reply — should be deduped
                {
                    "type": "message", "user": "U001", "text": "should we use sqlite?",
                    "ts": parent_ts, "thread_ts": parent_ts,
                },
                {
                    "type": "message", "user": "U002", "text": "yes for v0",
                    "ts": "1700000100.000300", "thread_ts": parent_ts,
                },
                {
                    "type": "message", "user": "U001", "text": "agreed",
                    "ts": "1700000200.000400", "thread_ts": parent_ts,
                },
            ]
        },
    )
    messages = list(client.iter_messages("engineering"))
    # parent + lunch + 2 replies = 4, NOT 5 (parent should only appear once)
    assert len(messages) == 4
    texts = [m.text for m in messages]
    assert texts.count("should we use sqlite?") == 1
    assert "yes for v0" in texts
    assert "agreed" in texts
    assert "lunch?" in texts


def test_iter_messages_drops_noise_subtypes() -> None:
    client, _ = make_client(
        channels=[{"id": "C001", "name": "general"}],
        history_by_channel={
            "C001": [
                {
                    "type": "message", "subtype": "channel_join",
                    "user": "U001", "text": "<@U001> joined", "ts": "1.0",
                },
                {
                    "type": "message", "user": "U001", "text": "real message",
                    "ts": "2.0",
                },
            ]
        },
    )
    messages = list(client.iter_messages("general"))
    assert len(messages) == 1
    assert messages[0].text == "real message"


def test_iter_messages_passes_since_as_oldest() -> None:
    client, fake = make_client(
        channels=[{"id": "C001", "name": "engineering"}],
        history_by_channel={"C001": []},
    )
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    list(client.iter_messages("engineering", since=since))
    history_calls = [c for c in fake.calls if c[0] == "conversations_history"]
    assert len(history_calls) == 1
    assert history_calls[0][1]["oldest"] == f"{since.timestamp():.6f}"


def test_iter_messages_raises_channel_not_accessible_for_unknown_channel() -> None:
    client, _ = make_client(channels=[])
    with pytest.raises(slack_api.ChannelNotAccessible) as exc_info:
        list(client.iter_messages("doesnotexist"))
    assert exc_info.value.channel == "doesnotexist"
    assert exc_info.value.slack_error == "channel_not_found"


def test_iter_messages_raises_channel_not_accessible_on_not_in_channel() -> None:
    """Slack returns not_in_channel from conversations.history when the bot
    is not a member of an existing channel. We convert that to ChannelNotAccessible."""
    from slack_sdk.errors import SlackApiError

    class FakeFailingClient(FakeWebClient):
        def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("conversations_history", kwargs))
            raise SlackApiError(
                "channel is private to verbatim",
                response={"error": "not_in_channel"},
            )

    client = slack_api.SlackClient(token="xoxb-test-token")
    client._client = FakeFailingClient(  # type: ignore[assignment]
        channels=[{"id": "C001", "name": "all-zakrs-tech"}],
    )
    with pytest.raises(slack_api.ChannelNotAccessible) as exc_info:
        list(client.iter_messages("all-zakrs-tech"))
    assert exc_info.value.channel == "all-zakrs-tech"
    assert exc_info.value.slack_error == "not_in_channel"
    assert "invite" in exc_info.value.hint.lower()


def test_iter_units_skips_inaccessible_channels_via_callback() -> None:
    """One bad channel must not abort the whole run when on_channel_error is given."""
    from slack_sdk.errors import SlackApiError

    parent_ts = "1700000000.000100"

    class HalfBrokenClient(FakeWebClient):
        def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
            chan = kwargs["channel"]
            self.calls.append(("conversations_history", kwargs))
            if chan == "C002":  # the "broken" channel
                raise SlackApiError("bot not in", response={"error": "not_in_channel"})
            return {
                "messages": self.history_by_channel.get(chan, []),
                "response_metadata": {"next_cursor": ""},
            }

    client = slack_api.SlackClient(token="xoxb-test-token")
    client._client = HalfBrokenClient(  # type: ignore[assignment]
        users=[{"id": "U001", "profile": {"display_name_normalized": "qat"}}],
        channels=[
            {"id": "C001", "name": "good-channel"},
            {"id": "C002", "name": "no-access"},
        ],
        history_by_channel={
            "C001": [{
                "type": "message", "user": "U001", "text": "do x?",
                "ts": parent_ts, "thread_ts": parent_ts, "reply_count": 2,
            }],
        },
        replies_by_parent={
            parent_ts: [
                {"type": "message", "user": "U001", "text": "do x?",
                 "ts": parent_ts, "thread_ts": parent_ts},
                {"type": "message", "user": "U001", "text": "yes",
                 "ts": "1700000100.000300", "thread_ts": parent_ts},
                {"type": "message", "user": "U001", "text": "ok",
                 "ts": "1700000200.000400", "thread_ts": parent_ts},
            ]
        },
    )

    skipped: list[slack_api.ChannelNotAccessible] = []
    units = list(client.iter_units(
        channels=["good-channel", "no-access"],
        min_thread_messages=3,
        on_channel_error=skipped.append,
    ))
    # We got the unit from the good channel
    assert len(units) == 1
    assert units[0].channel == "good-channel"
    # And we got told about the bad one
    assert len(skipped) == 1
    assert skipped[0].channel == "no-access"
    assert skipped[0].slack_error == "not_in_channel"


def test_iter_units_reraises_when_no_callback_provided() -> None:
    """Without a callback, fail-fast behaviour is preserved."""
    from slack_sdk.errors import SlackApiError

    class BrokenClient(FakeWebClient):
        def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("conversations_history", kwargs))
            raise SlackApiError("nope", response={"error": "not_in_channel"})

    client = slack_api.SlackClient(token="xoxb-test-token")
    client._client = BrokenClient(  # type: ignore[assignment]
        channels=[{"id": "C001", "name": "no-access"}],
    )
    with pytest.raises(slack_api.ChannelNotAccessible):
        list(client.iter_units(channels=["no-access"]))


# ----- unit assembly via shared builder -----


def test_iter_units_combines_history_and_replies_into_threads() -> None:
    parent_ts = "1700000000.000100"
    client, _ = make_client(
        users=[{"id": "U001", "profile": {"display_name_normalized": "qat"}},
               {"id": "U002", "profile": {"display_name_normalized": "jason"}}],
        channels=[{"id": "C001", "name": "engineering"}],
        history_by_channel={
            "C001": [
                {
                    "type": "message", "user": "U001",
                    "text": "should we use sqlite?",
                    "ts": parent_ts, "thread_ts": parent_ts, "reply_count": 2,
                },
            ]
        },
        replies_by_parent={
            parent_ts: [
                {"type": "message", "user": "U001", "text": "should we use sqlite?",
                 "ts": parent_ts, "thread_ts": parent_ts},
                {"type": "message", "user": "U002", "text": "yes",
                 "ts": "1700000100.000300", "thread_ts": parent_ts},
                {"type": "message", "user": "U001", "text": "ok",
                 "ts": "1700000200.000400", "thread_ts": parent_ts},
            ]
        },
    )
    units = list(client.iter_units(channels=["engineering"], min_thread_messages=3))
    assert len(units) == 1
    u = units[0]
    assert u.kind == "thread"
    assert u.channel == "engineering"
    assert len(u.messages) == 3
    # user map applied
    assert "@qat:" in u.transcript
    assert "@jason:" in u.transcript


def test_iter_units_respects_min_thread_messages() -> None:
    parent_ts = "1700000000.000100"
    client, _ = make_client(
        channels=[{"id": "C001", "name": "engineering"}],
        history_by_channel={
            "C001": [
                {"type": "message", "user": "U001", "text": "?",
                 "ts": parent_ts, "thread_ts": parent_ts, "reply_count": 1},
            ]
        },
        replies_by_parent={
            parent_ts: [
                {"type": "message", "user": "U001", "text": "?",
                 "ts": parent_ts, "thread_ts": parent_ts},
                {"type": "message", "user": "U002", "text": "no",
                 "ts": "1700000100.000300", "thread_ts": parent_ts},
            ]
        },
    )
    units = list(client.iter_units(channels=["engineering"], min_thread_messages=3))
    assert units == []  # only 2 messages in thread, below threshold


def test_iter_units_uses_all_channels_when_none_specified() -> None:
    client, fake = make_client(
        channels=[
            {"id": "C001", "name": "alpha"},
            {"id": "C002", "name": "beta"},
        ],
        history_by_channel={"C001": [], "C002": []},
    )
    list(client.iter_units(min_thread_messages=3))
    history_calls = [c for c in fake.calls if c[0] == "conversations_history"]
    assert {c[1]["channel"] for c in history_calls} == {"C001", "C002"}
