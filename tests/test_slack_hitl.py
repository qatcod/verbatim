"""Tests for the Slack interactive HITL feature (extraction card + block_actions)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from verbatim import slack_bot, state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# ----- fixtures -----


class FakeWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        return _FakeResponse({"ok": True, "ts": "1700000000.000100", **kwargs})

    def chat_postEphemeral(self, **kwargs: Any) -> Any:
        self.ephemerals.append(kwargs)
        return _FakeResponse({"ok": True, **kwargs})


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeHttpResponse()


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _FakeHttpResponse:
    def raise_for_status(self) -> None:
        return None


def _seed_commitment(db_path: Path, *, deliverable: str = "ship v0") -> str:
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="seed", participants=["Qat"],
            commitments=[Commitment(
                actor="Qat", deliverable=deliverable, deadline="Friday",
                confidence=Confidence.HIGH,
                sources=[SourceReference(
                    verbatim_quote="I'll ship Friday.",
                    speaker="Qat", rationale="explicit",
                )],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="m.txt")
        row = conn.execute(
            "SELECT id FROM entities WHERE primary_actor='Qat' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


# ----- block kit card builder -----


def test_card_has_verbatim_header(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    blocks = slack_bot.build_extraction_card_blocks(entity)
    text = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "verbatim" in text.lower()
    assert "Commitment" in text
    assert f"VRB-{eid[:8]}" in text


def test_card_has_four_action_buttons(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    blocks = slack_bot.build_extraction_card_blocks(entity)
    action_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(action_blocks) == 1
    btns = action_blocks[0]["elements"]
    assert len(btns) == 4
    action_ids = {b["action_id"] for b in btns}
    assert action_ids == {
        f"verbatim:confirm:{eid}",
        f"verbatim:dismiss:{eid}",
        f"verbatim:edit:{eid}",
        f"verbatim:reassign:{eid}",
    }


def test_card_includes_verbatim_quote(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    blocks = slack_bot.build_extraction_card_blocks(entity)
    section_texts = [
        b["text"]["text"] for b in blocks
        if b["type"] == "section" and "text" in b
    ]
    joined = "\n".join(section_texts)
    assert "I&#x27;ll ship Friday." in joined or "I'll ship Friday." in joined


def test_card_no_action_buttons_after_resolution(tmp_db_path: Path) -> None:
    """Once an entity is confirmed/dismissed/resolved, the buttons are hidden."""
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        store.update_entity_status(conn, eid, "confirmed")
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    blocks = slack_bot.build_extraction_card_blocks(entity)
    assert all(b["type"] != "actions" for b in blocks)


def test_card_escapes_html_in_user_content(tmp_db_path: Path) -> None:
    """A malicious deliverable/quote can't break Slack mrkdwn."""
    conn = state.open_db(tmp_db_path)
    try:
        result = ExtractionResult(
            meeting_summary="seed", participants=["Qat"],
            commitments=[Commitment(
                actor="<script>", deliverable="</section><b>bold</b>",
                confidence=Confidence.HIGH,
                sources=[SourceReference(
                    verbatim_quote="<b>not bold</b>", speaker="<i>x</i>",
                    rationale="r",
                )],
            )],
        )
        diag = ExtractionDiagnostics(model="t", input_tokens=1, output_tokens=1,
                                      stop_reason="end_turn", transcript_chars=10)
        state.save_extraction(conn, result, diag, source_path="m.txt")
        eid = conn.execute(
            "SELECT id FROM entities ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["id"]
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    blocks = slack_bot.build_extraction_card_blocks(entity)
    joined = str(blocks)
    assert "&lt;script&gt;" in joined or "&lt;b&gt;" in joined
    assert "<script>" not in joined


# ----- action dispatch (Confirm / Dismiss / Edit / Reassign) -----


def test_dispatch_confirm_marks_entity_confirmed(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="confirm", entity_id=eid, user_id="U001")
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["status"] == "confirmed"
    assert "Confirmed" in reply
    assert "<@U001>" in reply  # user attribution rendered


def test_dispatch_dismiss_marks_entity_dismissed(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="dismiss", entity_id=eid, user_id=None)
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["status"] == "dismissed"
    assert "Dismissed" in reply


def test_dispatch_edit_says_not_yet_implemented(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="edit", entity_id=eid, user_id="U001")
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    # Edit doesn't change state
    assert entity["status"] == "open"
    assert "not wired up" in reply.lower() or "isn't wired up" in reply.lower()


def test_dispatch_reassign_says_not_yet_implemented(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="reassign", entity_id=eid, user_id="U001")
    finally:
        conn.close()
    assert "not wired up" in reply.lower() or "isn't wired up" in reply.lower()


def test_dispatch_unknown_verb_returns_clear_error(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="garbage", entity_id=eid, user_id=None)
    finally:
        conn.close()
    assert "Unknown action" in reply


def test_dispatch_on_missing_entity(tmp_db_path: Path) -> None:
    state.open_db(tmp_db_path).close()
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.dispatch_action(conn, verb="confirm", entity_id="deadbeef", user_id="U001")
    finally:
        conn.close()
    assert "not found" in reply


# ----- handle_interactive end-to-end -----


def _make_bot_with_db(db_path: Path):
    fake_web = FakeWebClient()
    fake_http = FakeHttpClient()

    class FakeSocket:
        socket_mode_request_listeners: list = []

    bot = slack_bot.VerbatimSlackBot(
        bot_token="xoxb-x", app_token="",
        web_client=fake_web, socket_client=FakeSocket(),
        http_client=fake_http, db_path=db_path,
    )
    return bot, fake_web, fake_http


def test_handle_interactive_confirm_via_response_url(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    payload = {
        "type": "block_actions",
        "actions": [{"action_id": f"verbatim:confirm:{eid}", "value": eid}],
        "user": {"id": "U001"},
        "channel": {"id": "C001"},
        "response_url": "https://hooks.slack.com/actions/T01/x/y",
    }
    bot._handle_interactive(payload)
    # Posted via response_url with the confirmation reply text
    assert len(fake_http.posts) == 1
    body = fake_http.posts[0]["json"]
    assert "Confirmed" in body["text"]
    # And the DB was updated
    conn = state.open_db(tmp_db_path)
    try:
        assert store.fetch_entity(conn, eid)["status"] == "confirmed"
    finally:
        conn.close()


def test_handle_interactive_ignores_non_block_actions(tmp_db_path: Path) -> None:
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_interactive({"type": "view_submission", "actions": []})
    assert fake_http.posts == []
    assert fake_web.posts == []
    assert fake_web.ephemerals == []


def test_handle_interactive_ignores_unknown_action_id(tmp_db_path: Path) -> None:
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_interactive({
        "type": "block_actions",
        "actions": [{"action_id": "totally_unrelated_button"}],
        "user": {"id": "U001"},
        "channel": {"id": "C001"},
    })
    assert fake_http.posts == []
    assert fake_web.posts == []


def test_handle_interactive_falls_back_to_ephemeral_when_no_response_url(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    bot, fake_web, fake_http = _make_bot_with_db(tmp_db_path)
    bot._handle_interactive({
        "type": "block_actions",
        "actions": [{"action_id": f"verbatim:dismiss:{eid}"}],
        "user": {"id": "U001"},
        "channel": {"id": "C001"},
        # no response_url
    })
    # Used the ephemeral channel fallback
    assert len(fake_web.ephemerals) == 1
    assert fake_web.ephemerals[0]["channel"] == "C001"
    assert fake_web.ephemerals[0]["user"] == "U001"


# ----- post_extraction_card -----


def test_post_extraction_card_sends_blocks_to_channel(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    bot, fake_web, _ = _make_bot_with_db(tmp_db_path)
    bot.post_extraction_card("C001", eid)
    assert len(fake_web.posts) == 1
    post = fake_web.posts[0]
    assert post["channel"] == "C001"
    assert "blocks" in post
    assert any("verbatim" in str(b).lower() for b in post["blocks"])


def test_post_extraction_card_missing_entity(tmp_db_path: Path) -> None:
    bot, _, _ = _make_bot_with_db(tmp_db_path)
    with pytest.raises(ValueError):
        bot.post_extraction_card("C001", "deadbeef-nope")
