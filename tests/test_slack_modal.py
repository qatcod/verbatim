"""Tests for v0.10.1 — Slack interactive HITL Edit modal + Reassign picker.

Covers the new modal-opening flow (`_open_modal`, `views_open`), the
view-submission flow (`_handle_view_submission`, `apply_edit`,
`apply_reassign`), and the audit trail written into `entity_audit`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from verbatim import slack_bot, state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)

# ----- helpers -----


class FakeResp:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class FakeWeb:
    def __init__(self) -> None:
        self.opened_views: list[dict[str, Any]] = []
        self.dm_posts: list[dict[str, Any]] = []
        self.users_info_calls: list[str] = []
        self._users: dict[str, dict[str, Any]] = {}

    def add_user(self, uid: str, *, display: str | None = None,
                 real: str | None = None) -> None:
        self._users[uid] = {
            "name": uid,
            "profile": {
                "display_name_normalized": display,
                "display_name": display,
                "real_name_normalized": real,
                "real_name": real,
            },
        }

    def views_open(self, *, trigger_id: str, view: dict[str, Any]) -> Any:
        self.opened_views.append({"trigger_id": trigger_id, "view": view})
        return FakeResp({"ok": True})

    def conversations_open(self, *, users: str) -> Any:
        return FakeResp({"ok": True, "channel": {"id": f"D-{users}"}})

    def chat_postMessage(self, **kwargs: Any) -> Any:
        self.dm_posts.append(kwargs)
        return FakeResp({"ok": True})

    def chat_postEphemeral(self, **kwargs: Any) -> Any:
        return FakeResp({"ok": True})

    def users_info(self, *, user: str) -> Any:
        self.users_info_calls.append(user)
        if user in self._users:
            return FakeResp({"ok": True, "user": self._users[user]})
        return FakeResp({"ok": False, "error": "user_not_found"})


def _seed_commitment(db_path: Path, *, deliverable: str = "ship v0",
                     actor: str = "Qat") -> str:
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="seed", participants=[actor],
            commitments=[Commitment(
                actor=actor, deliverable=deliverable, deadline="Friday",
                confidence=Confidence.HIGH,
                sources=[SourceReference(
                    verbatim_quote="I'll ship Friday.",
                    speaker=actor, rationale="r",
                )],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="m.txt")
        row = conn.execute(
            "SELECT id FROM entities WHERE primary_actor=? ORDER BY created_at DESC LIMIT 1",
            (actor,),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _seed_question(db_path: Path) -> str:
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="seed", participants=["Taz"],
            open_questions=[OpenQuestion(
                topic="staffing", question="Who runs ops?",
                raised_by="Taz", confidence=Confidence.MEDIUM,
                sources=[SourceReference(
                    verbatim_quote="Who runs ops?",
                    speaker="Taz", rationale="r",
                )],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="m.txt")
        return conn.execute(
            "SELECT id FROM entities WHERE kind='open_question' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["id"]
    finally:
        conn.close()


def _seed_blocker(db_path: Path) -> str:
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="seed", participants=["Qat"],
            blockers=[Blocker(
                blocked_thing="launch", blocked_by="security review",
                owner="Qat", confidence=Confidence.LOW,
                sources=[SourceReference(
                    verbatim_quote="security first.",
                    speaker="Qat", rationale="r",
                )],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="m.txt")
        return conn.execute(
            "SELECT id FROM entities WHERE kind='blocker' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["id"]
    finally:
        conn.close()


# ----- modal view builders -----


def test_build_edit_modal_view_has_kind_specific_inputs(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    view = slack_bot.build_edit_modal_view(entity)
    assert view["type"] == "modal"
    assert view["callback_id"] == f"verbatim:edit:{eid}"
    action_ids = [
        elem["element"]["action_id"]
        for elem in view["blocks"]
        if elem.get("type") == "input"
    ]
    assert "deliverable" in action_ids
    assert "actor" in action_ids
    assert "deadline" in action_ids
    assert "note" in action_ids


def test_build_edit_modal_view_prefills_current_values(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, deliverable="ship the Xero pilot")
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    view = slack_bot.build_edit_modal_view(entity)
    deliv = next(
        b for b in view["blocks"]
        if b.get("type") == "input"
        and b["element"].get("action_id") == "deliverable"
    )
    assert deliv["element"]["initial_value"] == "ship the Xero pilot"


def test_build_reassign_modal_view_has_users_select(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        entity = state.show_entity(conn, eid)
    finally:
        conn.close()
    view = slack_bot.build_reassign_modal_view(entity)
    assert view["callback_id"] == f"verbatim:reassign:{eid}"
    types = [
        b["element"]["type"] for b in view["blocks"]
        if b.get("type") == "input"
    ]
    assert "users_select" in types


# ----- apply_edit -----


def test_apply_edit_updates_deliverable_and_payload(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, deliverable="old text")
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.apply_edit(
            conn, entity_id=eid,
            submitted_values={"deliverable": "new shipped text"},
            user_id="U001", user_label=None, note="rewrite for clarity",
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert "Edited" in reply
    assert "deliverable" in reply
    assert entity["payload"]["deliverable"] == "new shipped text"


def test_apply_edit_mirrors_actor_to_primary_column(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, actor="Qat")
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_edit(
            conn, entity_id=eid,
            submitted_values={"actor": "Jason"},
            user_id="U001", user_label=None, note=None,
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["primary_actor"] == "Jason"
    assert entity["payload"]["actor"] == "Jason"


def test_apply_edit_records_audit_row(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_edit(
            conn, entity_id=eid,
            submitted_values={"deliverable": "updated"},
            user_id="U042", user_label=None, note="hi",
        )
        audit = store.fetch_audit(conn, eid)
    finally:
        conn.close()
    assert len(audit) == 1
    assert audit[0]["action"] == "edit"
    assert audit[0]["actor_id"] == "U042"
    assert audit[0]["note"] == "hi"
    assert audit[0]["before"]["payload"]["deliverable"] != "updated"
    assert audit[0]["after"]["payload"]["deliverable"] == "updated"


def test_apply_edit_skips_empty_inputs(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, deliverable="original")
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_edit(
            conn, entity_id=eid,
            submitted_values={"deliverable": "", "actor": "  "},
            user_id="U001", user_label=None, note=None,
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    # Empty strings are now filtered out earlier; whitespace-only strings
    # still hit apply_edit but get stored as-is. We just confirm the
    # original survived for the empty case.
    assert entity["payload"]["deliverable"] == "original"


def test_apply_edit_missing_entity_returns_error(tmp_db_path: Path) -> None:
    conn = state.open_db(tmp_db_path)
    try:
        reply = slack_bot.apply_edit(
            conn, entity_id="deadbeef" * 4,
            submitted_values={"deliverable": "x"},
            user_id=None, user_label=None, note=None,
        )
    finally:
        conn.close()
    assert "not found" in reply.lower()


# ----- apply_reassign -----


def test_apply_reassign_updates_primary_actor(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, actor="Qat")
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_reassign(
            conn, entity_id=eid,
            new_actor_label="Taz", slack_user_id="UTAZ",
            submitter_id="U001", note=None,
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["primary_actor"] == "Taz"
    assert entity["payload"]["actor"] == "Taz"


def test_apply_reassign_mirrors_to_blocker_owner(tmp_db_path: Path) -> None:
    eid = _seed_blocker(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_reassign(
            conn, entity_id=eid,
            new_actor_label="Jason", slack_user_id="UJSN",
            submitter_id="U001", note=None,
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["payload"]["owner"] == "Jason"


def test_apply_reassign_mirrors_to_question_raised_by(tmp_db_path: Path) -> None:
    eid = _seed_question(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_reassign(
            conn, entity_id=eid,
            new_actor_label="Qat", slack_user_id="UQAT",
            submitter_id="U001", note=None,
        )
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["payload"]["raised_by"] == "Qat"


def test_apply_reassign_records_audit_with_slack_id(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.apply_reassign(
            conn, entity_id=eid,
            new_actor_label="Taz", slack_user_id="UTAZ",
            submitter_id="U001", note="needs new lead",
        )
        audit = store.fetch_audit(conn, eid)
    finally:
        conn.close()
    assert audit[0]["action"] == "reassign"
    assert "<@UTAZ>" in (audit[0]["note"] or "")
    assert "needs new lead" in (audit[0]["note"] or "")


# ----- audit on confirm/dismiss -----


def test_dispatch_confirm_writes_audit(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.dispatch_action(conn, verb="confirm", entity_id=eid, user_id="UQAT")
        audit = store.fetch_audit(conn, eid)
    finally:
        conn.close()
    actions = [a["action"] for a in audit]
    assert "confirm" in actions


def test_dispatch_dismiss_writes_audit(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    conn = state.open_db(tmp_db_path)
    try:
        slack_bot.dispatch_action(conn, verb="dismiss", entity_id=eid, user_id="UQAT")
        audit = store.fetch_audit(conn, eid)
    finally:
        conn.close()
    actions = [a["action"] for a in audit]
    assert "dismiss" in actions


# ----- input value extraction helpers -----


def test_extract_edit_values_strips_audit_note() -> None:
    state_values = {
        "verbatim_edit_deliverable": {
            "deliverable": {"type": "plain_text_input", "value": "new"}
        },
        "verbatim_edit_note": {
            "note": {"type": "plain_text_input", "value": "why"}
        },
    }
    out = slack_bot._extract_edit_values(state_values)
    assert out == {"deliverable": "new"}
    # note pulled separately via _extract_input_value
    assert slack_bot._extract_input_value(
        state_values, "verbatim_edit_note", "note",
    ) == "why"


def test_extract_input_value_returns_none_for_blank() -> None:
    state_values = {
        "block": {"act": {"value": "   "}},
        "block2": {"act": {"value": None}},
    }
    assert slack_bot._extract_input_value(state_values, "block", "act") is None
    assert slack_bot._extract_input_value(state_values, "block2", "act") is None
    assert slack_bot._extract_input_value(state_values, "missing", "act") is None


# ----- end-to-end: _open_modal + _handle_view_submission -----


def _make_bot(tmp_db_path: Path, web: FakeWeb) -> slack_bot.VerbatimSlackBot:
    """Construct a bot without connecting to Slack."""
    bot = slack_bot.VerbatimSlackBot.__new__(slack_bot.VerbatimSlackBot)
    bot._web = web
    bot._db_path = tmp_db_path
    bot._http = None
    return bot


def test_open_modal_for_edit_calls_views_open(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    web = FakeWeb()
    bot = _make_bot(tmp_db_path, web)
    bot._open_modal(verb="edit", entity_id=eid, trigger_id="TRIG1")
    assert len(web.opened_views) == 1
    call = web.opened_views[0]
    assert call["trigger_id"] == "TRIG1"
    assert call["view"]["callback_id"] == f"verbatim:edit:{eid}"


def test_open_modal_for_reassign_calls_views_open(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path)
    web = FakeWeb()
    bot = _make_bot(tmp_db_path, web)
    bot._open_modal(verb="reassign", entity_id=eid, trigger_id="TRIG2")
    assert len(web.opened_views) == 1
    assert web.opened_views[0]["view"]["callback_id"] == f"verbatim:reassign:{eid}"


def test_handle_view_submission_edit_applies_changes(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, deliverable="old")
    web = FakeWeb()
    bot = _make_bot(tmp_db_path, web)
    payload = {
        "type": "view_submission",
        "user": {"id": "UQAT"},
        "view": {
            "callback_id": f"verbatim:edit:{eid}",
            "state": {
                "values": {
                    "verbatim_edit_deliverable": {
                        "deliverable": {"value": "shiny new"}
                    },
                    "verbatim_edit_note": {"note": {"value": "tightened wording"}},
                },
            },
        },
    }
    bot._handle_view_submission(payload)
    conn = state.open_db(tmp_db_path)
    try:
        entity = store.fetch_entity(conn, eid)
        audit = store.fetch_audit(conn, eid)
    finally:
        conn.close()
    assert entity["payload"]["deliverable"] == "shiny new"
    assert audit[0]["action"] == "edit"
    # bot DM'd the submitter
    assert any("Edited" in (p.get("text") or "") for p in web.dm_posts)


def test_handle_view_submission_reassign_resolves_slack_user(tmp_db_path: Path) -> None:
    eid = _seed_commitment(tmp_db_path, actor="Qat")
    web = FakeWeb()
    web.add_user("UTAZ", display="Taz", real="Taz Y.")
    bot = _make_bot(tmp_db_path, web)
    payload = {
        "type": "view_submission",
        "user": {"id": "UQAT"},
        "view": {
            "callback_id": f"verbatim:reassign:{eid}",
            "state": {
                "values": {
                    "verbatim_reassign_user": {
                        "user": {"selected_user": "UTAZ"}
                    },
                    "verbatim_reassign_name": {"name": {"value": ""}},
                    "verbatim_reassign_note": {"note": {"value": ""}},
                },
            },
        },
    }
    bot._handle_view_submission(payload)
    conn = state.open_db(tmp_db_path)
    try:
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["primary_actor"] == "Taz"
    assert "UTAZ" in web.users_info_calls


def test_handle_view_submission_reassign_name_override_wins(tmp_db_path: Path) -> None:
    """Override name field beats the Slack-resolved name."""
    eid = _seed_commitment(tmp_db_path)
    web = FakeWeb()
    web.add_user("UTAZ", display="Taz")
    bot = _make_bot(tmp_db_path, web)
    payload = {
        "type": "view_submission",
        "user": {"id": "UQAT"},
        "view": {
            "callback_id": f"verbatim:reassign:{eid}",
            "state": {
                "values": {
                    "verbatim_reassign_user": {
                        "user": {"selected_user": "UTAZ"}
                    },
                    "verbatim_reassign_name": {"name": {"value": "Taz Y."}},
                    "verbatim_reassign_note": {"note": {"value": ""}},
                },
            },
        },
    }
    bot._handle_view_submission(payload)
    conn = state.open_db(tmp_db_path)
    try:
        entity = store.fetch_entity(conn, eid)
    finally:
        conn.close()
    assert entity["primary_actor"] == "Taz Y."
