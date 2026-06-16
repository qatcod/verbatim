"""Slack bot tests — command parsing, formatters, dispatch, digest, handler routing.

No network: WebClient is replaced with a FakeWebClient that records calls.
SocketModeClient is not exercised because its run loop is a thin shim around
Slack's WebSocket — testing the dispatch layer covers the meaningful logic.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from verbatim import slack_bot, state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)

# ----- fixtures -----


class FakeWebClient:
    """Minimal stand-in for slack_sdk.WebClient — records every call.

    `ephemeral_should_fail` simulates the `not_in_channel` Slack error to verify
    the fallback chain.
    """

    def __init__(self, *, ephemeral_should_fail: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.dm_opens: list[dict[str, Any]] = []
        self.ephemeral_should_fail = ephemeral_should_fail

    def chat_postMessage(self, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        return _FakeResponse({"ok": True, "ts": "1700000000.000100", **kwargs})

    def chat_postEphemeral(self, **kwargs: Any) -> Any:
        self.ephemerals.append(kwargs)
        if self.ephemeral_should_fail:
            raise RuntimeError("simulated not_in_channel")
        return _FakeResponse({"ok": True, **kwargs})

    def conversations_open(self, *, users: str) -> Any:
        self.dm_opens.append({"users": users})
        return _FakeResponse({"ok": True, "channel": {"id": "D-" + users}})


class FakeHttpClient:
    """Minimal stand-in for httpx.Client.post used by response_url path."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self.should_fail = should_fail

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.posts.append({"url": url, "json": json, "headers": headers})
        if self.should_fail:
            raise RuntimeError("simulated response_url failure")
        return _FakeHttpResponse()


class _FakeHttpResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeResponse:
    """Shape of slack_sdk's SlackResponse — supports both `.data` and dict-like get()."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


@pytest.fixture
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Insert one commitment, one decision, one question, one blocker."""
    result = ExtractionResult(
        meeting_summary="bot test seed",
        participants=["Qat", "Jason"],
        commitments=[Commitment(
            actor="Qat", deliverable="ship v0", deadline="EOD Wednesday",
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="I'll ship Wed.", speaker="Qat", rationale="r")],
        )],
        decisions=[Decision(
            topic="language", outcome="Python",
            participants=["Qat"], confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="Python it is.", speaker="Qat", rationale="r")],
        )],
        open_questions=[OpenQuestion(
            topic="cost", question="What's the API budget?",
            raised_by="Taz", addressed_to="Jason",
            confidence=Confidence.MEDIUM,
            sources=[SourceReference(verbatim_quote="budget?", speaker="Taz", rationale="r")],
        )],
        blockers=[Blocker(
            blocked_thing="ship public",
            blocked_by="extraction quality review",
            owner="Taz", confidence=Confidence.MEDIUM,
            sources=[SourceReference(verbatim_quote="don't ship half-baked", speaker="Jason", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model="test", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path="t.txt")
    return conn


# ----- parse_command_text -----


def test_parse_empty_returns_help() -> None:
    p = slack_bot.parse_command_text("")
    assert p.subcommand == "help"
    assert p.args == []


def test_parse_whitespace_returns_help() -> None:
    p = slack_bot.parse_command_text("   ")
    assert p.subcommand == "help"


def test_parse_simple_subcommand() -> None:
    p = slack_bot.parse_command_text("commitments")
    assert p.subcommand == "commitments"
    assert p.args == []


def test_parse_subcommand_with_args() -> None:
    p = slack_bot.parse_command_text("commitments qat")
    assert p.subcommand == "commitments"
    assert p.args == ["qat"]


def test_parse_is_case_insensitive_for_subcommand() -> None:
    p = slack_bot.parse_command_text("DECISIONS")
    assert p.subcommand == "decisions"


def test_parse_normalizes_questions_aliases() -> None:
    for raw in ("questions", "open-questions", "open_questions", "open"):
        assert slack_bot.parse_command_text(raw).subcommand == "questions"


# ----- formatters -----


def test_format_help_lists_commands() -> None:
    text = slack_bot.format_help()
    for tok in ("commitments", "decisions", "questions", "blockers", "stats", "show"):
        assert tok in text


def test_format_commitments_empty() -> None:
    assert "No open commitments" in slack_bot.format_commitments([])


def test_format_commitments_includes_actor_and_deadline() -> None:
    items = [{
        "id": "abc1234567",
        "kind": "commitment",
        "confidence": "high",
        "payload": {"actor": "Qat", "deliverable": "ship v0", "deadline": "Friday"},
        "sources": [],
    }]
    text = slack_bot.format_commitments(items)
    assert "*Qat*" in text
    assert "ship v0" in text
    assert "Friday" in text
    assert "abc12345" in text  # id prefix shown


def test_format_decisions_includes_topic_and_outcome() -> None:
    items = [{
        "id": "x",
        "kind": "decision",
        "confidence": "high",
        "payload": {"topic": "language", "outcome": "Python"},
        "sources": [],
    }]
    text = slack_bot.format_decisions(items)
    assert "language" in text
    assert "Python" in text


def test_format_stats_renders_counts() -> None:
    s = {"sessions": 7, "commitments_open": 4, "decisions_open": 2,
         "open_questions_open": 3, "blockers_open": 1,
         "entities_merged": 2, "projections_active": 1}
    text = slack_bot.format_stats(s)
    assert "7 sessions" in text
    assert "4 open commitments" in text
    assert "2 entities merged" in text


def test_format_entity_detail_includes_quotes(seeded_conn) -> None:
    items = state.list_commitments(seeded_conn)
    entity = state.show_entity(seeded_conn, items[0]["id"])
    text = slack_bot.format_entity_detail(entity)
    assert "I'll ship Wed." in text
    # v0.13.0 — detail view shows the short `#<code>` reference, not the full UUID.
    assert f"#{entity['code']}" in text


# ----- dispatch_command -----


def test_dispatch_help(seeded_conn) -> None:
    out = slack_bot.dispatch_command(slack_bot.ParsedCommand(subcommand="help", args=[]), seeded_conn)
    assert "/verbatim" in out


def test_dispatch_stats(seeded_conn) -> None:
    out = slack_bot.dispatch_command(slack_bot.ParsedCommand(subcommand="stats", args=[]), seeded_conn)
    assert "1 open commitments" in out
    assert "1 decisions" in out


def test_dispatch_commitments(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="commitments", args=[]), seeded_conn,
    )
    assert "Qat" in out
    assert "ship v0" in out


def test_dispatch_commitments_filtered_by_actor(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="commitments", args=["jason"]), seeded_conn,
    )
    assert "No open commitments" in out


def test_dispatch_decisions(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="decisions", args=[]), seeded_conn,
    )
    assert "Python" in out


def test_dispatch_questions(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="questions", args=[]), seeded_conn,
    )
    assert "What's the API budget?" in out


def test_dispatch_blockers(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="blockers", args=[]), seeded_conn,
    )
    assert "ship public" in out


def test_dispatch_show_with_unknown_prefix(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="show", args=["deadbeef"]), seeded_conn,
    )
    assert "No entity matches" in out


def test_dispatch_show_without_arg(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="show", args=[]), seeded_conn,
    )
    assert "Usage" in out


def test_dispatch_show_finds_entity(seeded_conn) -> None:
    items = state.list_commitments(seeded_conn)
    prefix = items[0]["id"][:8]
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="show", args=[prefix]), seeded_conn,
    )
    assert "I'll ship Wed." in out


def test_dispatch_unknown_subcommand_falls_through_to_help(seeded_conn) -> None:
    out = slack_bot.dispatch_command(
        slack_bot.ParsedCommand(subcommand="garbage", args=[]), seeded_conn,
    )
    assert "Unknown subcommand" in out
    assert "/verbatim" in out


# ----- VerbatimSlackBot constructor -----


def test_bot_requires_bot_token() -> None:
    with pytest.raises(ValueError):
        slack_bot.VerbatimSlackBot(bot_token="", app_token="xapp-x")


def test_bot_requires_app_token_unless_socket_injected() -> None:
    fake_web = FakeWebClient()
    with pytest.raises(ValueError):
        slack_bot.VerbatimSlackBot(bot_token="xoxb-x", app_token="", web_client=fake_web)


def test_bot_constructor_accepts_injected_clients() -> None:
    fake_web = FakeWebClient()

    class FakeSocket:
        socket_mode_request_listeners: list = []

    bot = slack_bot.VerbatimSlackBot(
        bot_token="xoxb-x", app_token="", web_client=fake_web, socket_client=FakeSocket(),
    )
    assert bot is not None


# ----- slash command handling end-to-end -----


def _make_bot_with_db(
    db_path,
    *,
    web_fails: bool = False,
    http_fails: bool = False,
) -> tuple[slack_bot.VerbatimSlackBot, FakeWebClient, FakeHttpClient]:
    fake_web = FakeWebClient(ephemeral_should_fail=web_fails)
    fake_http = FakeHttpClient(should_fail=http_fails)

    class FakeSocket:
        socket_mode_request_listeners: list = []

    bot = slack_bot.VerbatimSlackBot(
        bot_token="xoxb-x", app_token="",
        web_client=fake_web, socket_client=FakeSocket(),
        http_client=fake_http,
        db_path=db_path,
    )
    return bot, fake_web, fake_http


def test_handle_slash_command_uses_response_url_by_default(tmp_db_path, seeded_conn) -> None:
    seeded_conn.close()  # close fixture connection so the bot can open its own
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_slash_command({
        "text": "commitments",
        "user_id": "U001",
        "channel_id": "C001",
        "response_url": "https://hooks.slack.com/commands/T01/x/y",
    })
    # response_url path used — no Web API calls needed
    assert len(fake_http.posts) == 1
    post = fake_http.posts[0]
    assert post["url"] == "https://hooks.slack.com/commands/T01/x/y"
    assert post["json"]["response_type"] == "ephemeral"
    assert "Qat" in post["json"]["text"]
    # Web API not touched
    assert fake_web.ephemerals == []
    assert fake_web.posts == []


def test_response_url_failure_falls_back_to_ephemeral(tmp_db_path, seeded_conn) -> None:
    seeded_conn.close()
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path, http_fails=True)
    bot._handle_slash_command({
        "text": "commitments",
        "user_id": "U001",
        "channel_id": "C001",
        "response_url": "https://hooks.slack.com/commands/T01/x/y",
    })
    # http failed → ephemeral attempt → success
    assert len(fake_http.posts) == 1
    assert len(fake_web.ephemerals) == 1
    assert fake_web.ephemerals[0]["channel"] == "C001"


def test_ephemeral_failure_falls_back_to_dm(tmp_db_path, seeded_conn) -> None:
    """If both response_url and chat.postEphemeral fail (not_in_channel etc.),
    the bot should DM the user as a last-ditch reply path."""
    seeded_conn.close()
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path, http_fails=True, web_fails=True)
    bot._handle_slash_command({
        "text": "commitments",
        "user_id": "U001",
        "channel_id": "C001",
        "response_url": "https://hooks.slack.com/commands/T01/x/y",
    })
    # DM was opened
    assert len(fake_web.dm_opens) == 1
    assert fake_web.dm_opens[0]["users"] == "U001"
    # Message posted to the DM channel
    dm_posts = [p for p in fake_web.posts if p["channel"].startswith("D-")]
    assert len(dm_posts) == 1


def test_handle_slash_command_no_response_url_falls_through_to_web(tmp_db_path, seeded_conn) -> None:
    """Payloads without response_url (rare) should still work via Web API."""
    seeded_conn.close()
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_slash_command({
        "text": "stats",
        "channel_id": "C001",
        # no response_url, no user_id
    })
    assert fake_http.posts == []
    assert len(fake_web.posts) == 1
    assert fake_web.posts[0]["channel"] == "C001"


def test_handle_slash_command_no_channel_is_noop(tmp_db_path) -> None:
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_slash_command({"text": "stats"})
    assert fake_http.posts == []
    assert fake_web.posts == []
    assert fake_web.ephemerals == []


# ----- digest -----


def test_post_digest_includes_stats_and_recent_items(tmp_db_path, seeded_conn) -> None:
    seeded_conn.close()
    bot, fake_web, _ = _make_bot_with_db(tmp_db_path)
    bot.post_digest("C001")
    assert len(fake_web.posts) == 1
    text = fake_web.posts[0]["text"]
    assert "Verbatim digest" in text
    assert "open commitments" in text
    assert "Qat" in text  # recent commitments section


def test_post_digest_empty_state(tmp_db_path) -> None:
    bot, fake_web, _ = _make_bot_with_db(tmp_db_path)
    bot.post_digest("C001")
    text = fake_web.posts[0]["text"]
    assert "0 open commitments" in text
