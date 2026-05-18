"""Linear projection tests — GraphQL via httpx.MockTransport; no network."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from verbatim import state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.projections import linear as linear_proj
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# ----- fixtures -----


def make_linear_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> linear_proj.LinearClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        headers={
            "Authorization": "lin_api_test",
            "Content-Type": "application/json",
            "User-Agent": "verbatim/test",
        },
    )
    return linear_proj.LinearClient(api_key="lin_api_test", client=http)


def gql_response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"data": data}).encode())


def gql_error(errors: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"errors": errors, "data": None}).encode())


def _seed_commitment(
    conn: sqlite3.Connection,
    *,
    actor: str = "Qat",
    deliverable: str = "ship v0",
    deadline: str | None = "2026-05-23",
    confidence: Confidence = Confidence.HIGH,
) -> str:
    result = ExtractionResult(
        meeting_summary="seed",
        participants=[actor],
        commitments=[Commitment(
            actor=actor, deliverable=deliverable, deadline=deadline,
            confidence=confidence,
            sources=[SourceReference(
                verbatim_quote="I'll ship by friday.",
                speaker=actor, rationale="explicit",
                approximate_timestamp="10:30",
            )],
        )],
    )
    diag = ExtractionDiagnostics(
        model="test", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path="meeting.txt")
    row = conn.execute(
        "SELECT id FROM entities WHERE primary_actor = ? ORDER BY created_at DESC LIMIT 1",
        (actor,),
    ).fetchone()
    return row["id"]


# ----- LinearClient: constructor -----


def test_constructor_requires_api_key() -> None:
    with pytest.raises(ValueError):
        linear_proj.LinearClient(api_key="")


# ----- LinearClient: queries -----


def test_viewer_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "viewer" in body["query"]
        return gql_response({"viewer": {"id": "u1", "name": "Qat", "email": "q@x.com", "displayName": "qat"}})

    client = make_linear_client(handler)
    me = client.viewer()
    client.close()
    assert me["displayName"] == "qat"


def test_list_teams() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return gql_response({"teams": {"nodes": [
            {"id": "t1", "name": "Engineering", "key": "ENG"},
            {"id": "t2", "name": "Design", "key": "DES"},
        ]}})

    client = make_linear_client(handler)
    teams = client.list_teams()
    client.close()
    assert len(teams) == 2
    assert teams[0]["name"] == "Engineering"


def test_list_workflow_states_filters_by_team() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body.get("variables", {}))
        return gql_response({"workflowStates": {"nodes": [
            {"id": "s1", "name": "Backlog", "type": "backlog", "position": 0},
            {"id": "s2", "name": "Todo", "type": "unstarted", "position": 1},
        ]}})

    client = make_linear_client(handler)
    states = client.list_workflow_states("team-id-1")
    client.close()
    assert states[0]["name"] == "Backlog"
    assert captured[0] == {"id": "team-id-1"}


def test_graphql_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return gql_error([{"message": "Authentication required"}])

    client = make_linear_client(handler)
    with pytest.raises(RuntimeError):
        client.viewer()
    client.close()


# ----- LinearClient: mutation -----


def test_create_issue_returns_issue_payload() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        assert "issueCreate" in body["query"]
        inp = body["variables"]["input"]
        return gql_response({"issueCreate": {
            "success": True,
            "issue": {
                "id": "issue-uuid",
                "identifier": "ENG-42",
                "url": "https://linear.app/team/issue/ENG-42/test",
                "title": inp["title"],
            },
        }})

    client = make_linear_client(handler)
    issue = client.create_issue(
        team_id="t1", title="Test issue", description="body",
        assignee_id="u1", due_date="2026-05-23",
    )
    client.close()
    assert issue["identifier"] == "ENG-42"
    assert captured[0]["variables"]["input"]["teamId"] == "t1"
    assert captured[0]["variables"]["input"]["assigneeId"] == "u1"
    assert captured[0]["variables"]["input"]["dueDate"] == "2026-05-23"


def test_create_issue_non_success_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return gql_response({"issueCreate": {"success": False, "issue": None}})

    client = make_linear_client(handler)
    with pytest.raises(RuntimeError):
        client.create_issue(team_id="t1", title="x", description="x")
    client.close()


# ----- user resolver -----


def test_user_resolver_matches_display_name() -> None:
    users = [
        {"id": "u1", "displayName": "qat", "name": "Qat Hassan", "email": "qat@x.com"},
        {"id": "u2", "displayName": "jason", "name": "Jason R", "email": "jason@x.com"},
    ]
    resolve = linear_proj.build_user_resolver(users)
    assert resolve("qat") == "u1"
    assert resolve("Qat") == "u1"
    assert resolve("@qat") == "u1"
    assert resolve("jason") == "u2"


def test_user_resolver_falls_through_to_name_then_email() -> None:
    users = [{"id": "u1", "name": "Qat Hassan", "email": "qat-h@x.com"}]
    resolve = linear_proj.build_user_resolver(users)
    assert resolve("Qat Hassan") == "u1"
    assert resolve("qat-h") == "u1"
    assert resolve("unknown") is None


def test_user_resolver_returns_none_for_empty() -> None:
    resolve = linear_proj.build_user_resolver([])
    assert resolve(None) is None
    assert resolve("anyone") is None


# ----- issue rendering -----


def test_render_issue_from_commitment_includes_quote_and_id(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)
    entity = store.fetch_entity(conn, eid)
    draft = linear_proj.render_issue_from_commitment(entity)
    assert "Qat: ship v0" in draft.title
    assert "I'll ship by friday." in draft.description
    assert eid in draft.description  # entity id round-tripped
    assert draft.due_date == "2026-05-23"  # ISO date passes through


def test_render_issue_handles_unparseable_deadline(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn, deadline="EOD Friday")
    entity = store.fetch_entity(conn, eid)
    draft = linear_proj.render_issue_from_commitment(entity)
    assert draft.due_date is None  # we don't try to parse natural-language deadlines


def test_render_issue_no_actor_no_assignee_match(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn, actor="Unknown Person")
    entity = store.fetch_entity(conn, eid)
    resolve = linear_proj.build_user_resolver([{"id": "u1", "displayName": "someone"}])
    draft = linear_proj.render_issue_from_commitment(entity, assignee_resolver=resolve)
    assert draft.assignee_id is None


# ----- plan_projection -----


def test_plan_projection_returns_draft_for_pending(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)
    entity = store.fetch_entity(conn, eid)
    plan = linear_proj.plan_projection(conn, entity)
    assert plan.skip_reason is None
    assert plan.draft.title.startswith("Qat:")


def test_plan_projection_skips_non_canonical(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, deliverable="ship v0")
    id2 = _seed_commitment(conn, deliverable="ship v0 by EOD")
    from verbatim import reconcile
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    entity = store.fetch_entity(conn, id2)
    plan = linear_proj.plan_projection(conn, entity)
    assert plan.skip_reason is not None
    assert "non-canonical" in plan.skip_reason


def test_plan_projection_skips_already_projected(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)
    store.insert_projection(
        conn, entity_id=eid, target_kind=linear_proj.TARGET_KIND,
        external_id="issue-1", external_url="https://x",
    )
    entity = store.fetch_entity(conn, eid)
    plan = linear_proj.plan_projection(conn, entity)
    assert plan.skip_reason is not None
    assert "already projected" in plan.skip_reason


# ----- execute_projection (end-to-end with mocked Linear) -----


def test_execute_projection_creates_and_records(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return gql_response({"issueCreate": {
            "success": True,
            "issue": {
                "id": "issue-uuid", "identifier": "ENG-1",
                "url": "https://linear.app/x/ENG-1", "title": "x",
            },
        }})

    client = make_linear_client(handler)
    entity = store.fetch_entity(conn, eid)
    plan = linear_proj.plan_projection(conn, entity)
    info = linear_proj.execute_projection(conn, client, plan, team_id="t1", state_id="s1")
    client.close()

    assert info["external_id"] == "issue-uuid"
    assert info["identifier"] == "ENG-1"
    # projection persisted
    existing = store.find_active_projection(
        conn, entity_id=eid, target_kind=linear_proj.TARGET_KIND,
    )
    assert existing is not None
    assert existing["external_id"] == "issue-uuid"


def test_idempotency_second_plan_skips(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)

    def handler(_request: httpx.Request) -> httpx.Response:
        return gql_response({"issueCreate": {
            "success": True,
            "issue": {"id": "issue1", "identifier": "ENG-1", "url": "u", "title": "x"},
        }})

    client = make_linear_client(handler)
    entity = store.fetch_entity(conn, eid)
    plan1 = linear_proj.plan_projection(conn, entity)
    linear_proj.execute_projection(conn, client, plan1, team_id="t1")
    plan2 = linear_proj.plan_projection(conn, entity)
    client.close()
    assert plan2.skip_reason is not None  # second pass would skip


# ----- deactivate_projection -----


def test_deactivate_projection_flips_status(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)
    pid = store.insert_projection(
        conn, entity_id=eid, target_kind=linear_proj.TARGET_KIND,
        external_id="issue-1", external_url="u",
    )
    changed = linear_proj.deactivate_projection(conn, pid)
    assert changed is True
    # now find_active_projection should not return it
    assert store.find_active_projection(
        conn, entity_id=eid, target_kind=linear_proj.TARGET_KIND,
    ) is None


def test_deactivate_with_close_archives_linear_issue(conn: sqlite3.Connection) -> None:
    eid = _seed_commitment(conn)
    pid = store.insert_projection(
        conn, entity_id=eid, target_kind=linear_proj.TARGET_KIND,
        external_id="issue-1", external_url="u",
    )

    archived: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "issueArchive" in body["query"]
        archived.append(body["variables"]["id"])
        return gql_response({"issueArchive": {"success": True}})

    client = make_linear_client(handler)
    linear_proj.deactivate_projection(conn, pid, client=client, close_linear=True)
    client.close()
    assert archived == ["issue-1"]
