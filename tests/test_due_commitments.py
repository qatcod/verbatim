"""Tests for the `due_commitments` tool — the read the proactive layer is built on.

It answers two questions at once, and the second is the one that makes a nudge useful: WHAT is
outstanding, and WHERE was it promised. `session_external_id` is the caller's own id for the
transcript, which is how a nudge lands in the conversation the commitment was made in rather than in
a separate view nobody opens.
"""
import asyncio
import pathlib
from datetime import date, timedelta

import pytest

from verbatim import mcp_server, store
from verbatim.deadlines import DUE_SOON_WINDOW_DAYS


@pytest.fixture()
def conn(tmp_path: pathlib.Path):
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _session(conn, external_id: str | None = "convaa-t1") -> str:
    return store.insert_session(
        conn, source_path=None, source_kind="transcript", model="m",
        meeting_summary="s", participants=[], external_id=external_id,
    )


def _commitment(conn, session_id: str, *, actor: str, deliverable: str, deadline: str | None,
                confidence: str = "high") -> str:
    return store.insert_entity(
        conn, session_id=session_id, kind="commitment", confidence=confidence,
        payload={"actor": actor, "deliverable": deliverable, "deadline": deadline},
        primary_actor=actor, primary_topic=deliverable, deadline=deadline,
    )


def _call(conn, args: dict | None = None):
    return mcp_server._due_commitments(conn, args or {})


def _iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ----------------------- what comes back -----------------------


def test_overdue_and_due_soon_come_back_and_scheduled_does_not(conn):
    sid = _session(conn)
    _commitment(conn, sid, actor="dan", deliverable="ledger export", deadline=_iso(-3))
    _commitment(conn, sid, actor="ana", deliverable="credentials", deadline=_iso(0))
    _commitment(conn, sid, actor="mariam", deliverable="template", deadline=_iso(2))
    # Far in the future: real, tracked, and not something to nudge about today.
    _commitment(conn, sid, actor="priya", deliverable="pg16 migration", deadline=_iso(90))

    _text, out = _call(conn)
    got = {c["deliverable"]: c["due_status"] for c in out["commitments"]}
    assert got == {
        "ledger export": "overdue",
        "credentials": "due_today",
        "template": "due_soon",
    }


def test_overdue_first_then_soonest(conn):
    # A consumer rendering the list in order should get the right emphasis without knowing this
    # ordering exists.
    sid = _session(conn)
    _commitment(conn, sid, actor="a", deliverable="soon", deadline=_iso(5))
    _commitment(conn, sid, actor="b", deliverable="very overdue", deadline=_iso(-10))
    _commitment(conn, sid, actor="c", deliverable="today", deadline=_iso(0))
    _commitment(conn, sid, actor="d", deliverable="slightly overdue", deadline=_iso(-1))

    _text, out = _call(conn)
    assert [c["deliverable"] for c in out["commitments"]] == [
        "very overdue", "slightly overdue", "today", "soon",
    ]
    assert [c["days_until"] for c in out["commitments"]] == [-10, -1, 0, 5]


def test_it_carries_the_caller_s_own_session_id(conn):
    # THE field that makes a nudge land in the right conversation. Without it a consumer can tell
    # that something is overdue and not where it was promised.
    sid = _session(conn, external_id="convaa-transcript-42")
    _commitment(conn, sid, actor="dan", deliverable="export", deadline=_iso(-1))
    _text, out = _call(conn)
    assert out["commitments"][0]["session_external_id"] == "convaa-transcript-42"


def test_a_hand_run_ingest_has_no_external_id_and_that_is_not_an_error(conn):
    # `verbatim ingest` by hand sets no external_id. Such a commitment is still real and still
    # overdue; a consumer simply has nowhere to post about it, which is its decision to make.
    sid = _session(conn, external_id=None)
    _commitment(conn, sid, actor="dan", deliverable="export", deadline=_iso(-1))
    _text, out = _call(conn)
    assert len(out["commitments"]) == 1
    assert out["commitments"][0]["session_external_id"] is None


# ----------------------- what is excluded, and why -----------------------


def test_a_resolved_commitment_is_never_due(conn):
    sid = _session(conn)
    cid = _commitment(conn, sid, actor="dan", deliverable="done thing", deadline=_iso(-5))
    # Through the real API, which is what `resolve_entity` (the MCP tool) calls -- so this test
    # exercises the same transition a user marking something done would produce.
    assert store.update_entity_status(conn, cid, "resolved") is True
    _text, out = _call(conn)
    assert out["commitments"] == [], "nudging about a resolved commitment is the worst false positive here"


def test_a_merged_duplicate_is_nudged_about_once_under_its_canonical(conn):
    # Reconciliation exists so the same commitment said twice is one thing. Nudging about both would
    # make the feature feel broken in exactly the case reconciliation was built for.
    sid = _session(conn)
    canonical = _commitment(conn, sid, actor="dan", deliverable="ledger export", deadline=_iso(-2))
    dup = _commitment(conn, sid, actor="dan", deliverable="the ledger export", deadline=_iso(-2))
    store.set_canonical(conn, dup, canonical)

    _text, out = _call(conn)
    assert [c["id"] for c in out["commitments"]] == [canonical]


def test_a_commitment_with_no_deadline_is_neither_due_nor_unparsed(conn):
    # There is nothing to be late for. It must not appear in either list — putting it in `unparsed`
    # would tell a consumer a date failed to read when none was given.
    sid = _session(conn)
    _commitment(conn, sid, actor="dan", deliverable="think about it", deadline=None)
    _commitment(conn, sid, actor="ana", deliverable="also think", deadline="   ")
    _text, out = _call(conn)
    assert out["commitments"] == []
    assert out["unparsed"] == []


def test_an_unreadable_deadline_is_reported_not_dropped(conn):
    """The same choice parse_deadline makes by returning None rather than guessing.

    "when the audit clears" is a real commitment with a real deadline that no parser should invent a
    date for. Dropping it silently would make the list look complete when it is not.
    """
    sid = _session(conn)
    _commitment(conn, sid, actor="dan", deliverable="ship it", deadline="when the audit clears")
    _text, out = _call(conn)
    assert out["commitments"] == []
    assert len(out["unparsed"]) == 1
    assert out["unparsed"][0]["deadline_text"] == "when the audit clears"
    assert out["unparsed"][0]["deliverable"] == "ship it"


# ----------------------- the window -----------------------


def test_within_days_moves_the_boundary(conn):
    sid = _session(conn)
    _commitment(conn, sid, actor="a", deliverable="in 10 days", deadline=_iso(10))
    # Default window is 7, so 10 days out is 'scheduled' and absent.
    _text, out = _call(conn)
    assert out["commitments"] == []
    assert out["within_days"] == DUE_SOON_WINDOW_DAYS
    # Widen it and the same commitment appears.
    _text, out = _call(conn, {"within_days": 14})
    assert [c["deliverable"] for c in out["commitments"]] == ["in 10 days"]


def test_overdue_is_included_whatever_the_window(conn):
    # A window of 0 means "nothing is due soon", not "ignore what is already late".
    sid = _session(conn)
    _commitment(conn, sid, actor="a", deliverable="late", deadline=_iso(-4))
    _text, out = _call(conn, {"within_days": 0})
    assert [c["deliverable"] for c in out["commitments"]] == ["late"]


def test_actor_filter_is_case_insensitive(conn):
    sid = _session(conn)
    _commitment(conn, sid, actor="Dan", deliverable="his", deadline=_iso(-1))
    _commitment(conn, sid, actor="Ana", deliverable="hers", deadline=_iso(-1))
    _text, out = _call(conn, {"actor": "dan"})
    assert [c["deliverable"] for c in out["commitments"]] == ["his"]


# ----------------------- argument validation -----------------------


@pytest.mark.parametrize("args", [
    {"within_days": "7"},        # a string, which a JSON client can send
    {"within_days": 1.5},
    {"within_days": True},       # bool is an int in Python; it is not a window
    {"within_days": -1},
    {"within_days": 400},
    {"actor": 5},
])
def test_bad_arguments_are_rejected(conn, args):
    with pytest.raises(ValueError):
        _call(conn, args)


# ----------------------- the prose half -----------------------


def test_the_text_says_what_is_late_and_by_how_much(conn):
    sid = _session(conn)
    _commitment(conn, sid, actor="dan", deliverable="ledger export", deadline=_iso(-3))
    text, _out = _call(conn)
    assert "overdue" in text
    assert "dan" in text
    assert "ledger export" in text
    assert "3 day(s) overdue" in text


def test_the_text_is_explicit_when_nothing_is_due(conn):
    # "Nothing due" has to be distinguishable from "the tool failed to look".
    text, out = _call(conn)
    assert "Nothing due" in text
    assert out["commitments"] == [] and out["unparsed"] == []


def test_the_text_mentions_unreadable_deadlines(conn):
    sid = _session(conn)
    _commitment(conn, sid, actor="dan", deliverable="ship it", deadline="when the audit clears")
    text, _out = _call(conn)
    assert "could not be read" in text
    assert "when the audit clears" in text


# ----------------------- through the tool interface -----------------------


def test_the_tool_is_advertised_with_both_schemas():
    result = asyncio.run(mcp_server._list_tools(None, None))
    tool = next((t for t in result.tools if t.name == "due_commitments"), None)
    assert tool is not None
    assert set(tool.input_schema["properties"]) >= {"within_days", "actor"}
    # The output schema is a contract with a program; session_external_id is why it exists.
    assert tool.output_schema is not None
    assert "commitments" in tool.output_schema["required"]
    assert "unparsed" in tool.output_schema["required"]


def test_the_tool_result_carries_structured_content(tmp_path, monkeypatch):
    import mcp.types as types

    db = tmp_path / "s.db"
    monkeypatch.setenv("VERBATIM_DB_PATH", str(db))
    c = store.connect(db)
    try:
        sid = _session(c, external_id="convaa-t9")
        _commitment(c, sid, actor="dan", deliverable="export", deadline=_iso(-2))
    finally:
        c.close()

    async def _run():
        params = types.CallToolRequestParams(name="due_commitments", arguments={})
        return await mcp_server._call_tool(None, params)

    result = asyncio.run(_run())
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["commitments"][0]["session_external_id"] == "convaa-t9"
    assert "overdue" in result.content[0].text


def test_a_bad_argument_comes_back_as_a_tool_error_not_an_exception(tmp_path, monkeypatch):
    # mcp 2.x does not catch handler exceptions; _call_tool restores that, and a nudge job must get
    # a result it can log rather than a transport-level failure.
    import mcp.types as types

    monkeypatch.setenv("VERBATIM_DB_PATH", str(tmp_path / "s.db"))

    async def _run():
        params = types.CallToolRequestParams(name="due_commitments", arguments={"within_days": "soon"})
        return await mcp_server._call_tool(None, params)

    result = asyncio.run(_run())
    assert result.is_error
    assert "within_days" in result.content[0].text
