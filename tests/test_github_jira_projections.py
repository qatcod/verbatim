"""GitHub Issues + Jira projection tests — both mock httpx, no network."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from verbatim import reconcile, state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.projections import github_issues, jira
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# ----- shared fixtures -----


def _seed(conn: sqlite3.Connection, *, deliverable: str = "ship v0") -> str:
    result = ExtractionResult(
        meeting_summary="seed", participants=["Alice"],
        commitments=[Commitment(
            actor="Alice", deliverable=deliverable, deadline="2026-05-23",
            confidence=Confidence.HIGH,
            sources=[SourceReference(
                verbatim_quote="I'll ship Friday.",
                speaker="Alice", rationale="explicit",
            )],
        )],
    )
    diag = ExtractionDiagnostics(
        model="test", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path="m.txt")
    row = conn.execute(
        "SELECT id FROM entities WHERE primary_actor = 'Alice' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["id"]


# ============================================================================
# GitHub Issues
# ============================================================================


def gh_response(payload: dict, status: int = 201) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())


def make_gh_client(handler) -> github_issues.GitHubIssuesClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        headers={
            "Authorization": "Bearer test",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "verbatim/test",
        },
    )
    return github_issues.GitHubIssuesClient(token="test", client=http)


def test_github_constructor_requires_token() -> None:
    with pytest.raises(ValueError):
        github_issues.GitHubIssuesClient(token="")


def test_github_create_issue_returns_issue() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "method": request.method,
            "url": str(request.url),
            "body": json.loads(request.content) if request.content else None,
        })
        return gh_response({
            "id": 1234567, "number": 42,
            "html_url": "https://github.com/qatcod/verbatim/issues/42",
            "title": "[verbatim] Alice: ship v0",
        })

    client = make_gh_client(handler)
    issue = client.create_issue(
        repo="qatcod/verbatim",
        title="[verbatim] Alice: ship v0",
        body="body",
        assignees=["qatcod"],
        labels=["verbatim", "auto"],
    )
    client.close()
    assert issue["number"] == 42
    assert "github.com" in issue["html_url"]
    assert captured[0]["body"]["assignees"] == ["qatcod"]
    assert "verbatim" in captured[0]["body"]["labels"]


def test_github_render_includes_quote_and_id(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        entity = store.fetch_entity(conn, eid)
        draft = github_issues.render_issue_from_commitment(entity)
        assert "Alice: ship v0" in draft.title
        assert "[verbatim]" in draft.title
        assert "I'll ship Friday." in draft.body
        assert eid in draft.body
        assert "verbatim" in draft.labels
    finally:
        conn.close()


def test_github_plan_skips_non_canonical(tmp_path: Path) -> None:
    db = tmp_path / "g_merged.db"
    conn = state.open_db(db)
    try:
        id_a = _seed(conn, deliverable="ship v0")
        id_b = _seed(conn, deliverable="ship v0 by EOD")
        reconcile.link_entities(conn, canonical_id=id_a, member_id=id_b)
        merged_entity = store.fetch_entity(conn, id_b)
        plan = github_issues.plan_projection(conn, merged_entity)
        assert plan.skip_reason is not None
        assert "non-canonical" in plan.skip_reason
    finally:
        conn.close()


def test_github_idempotency_second_plan_skips(tmp_path: Path) -> None:
    db = tmp_path / "g_idem.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        entity = store.fetch_entity(conn, eid)

        def handler(request: httpx.Request) -> httpx.Response:
            return gh_response({
                "id": 1, "number": 7,
                "html_url": "https://github.com/x/y/issues/7", "title": "x",
            })

        client = make_gh_client(handler)
        plan1 = github_issues.plan_projection(conn, entity)
        github_issues.execute_projection(conn, client, plan1, repo="x/y")
        plan2 = github_issues.plan_projection(conn, entity)
        client.close()
        assert plan2.skip_reason is not None
        assert "already projected" in plan2.skip_reason
    finally:
        conn.close()


def test_github_close_issue_via_deactivate(tmp_path: Path) -> None:
    db = tmp_path / "g_close.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        pid = store.insert_projection(
            conn, entity_id=eid, target_kind=github_issues.TARGET_KIND,
            external_id="999", external_url="https://github.com/o/r/issues/7",
            metadata={"repo": "o/r", "number": 7},
        )

        archived: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                body = json.loads(request.content)
                archived.append(body.get("state"))
                return gh_response({"state": "closed"}, status=200)
            return httpx.Response(404)

        client = make_gh_client(handler)
        ok = github_issues.deactivate_projection(
            conn, pid, client=client, close_external=True,
        )
        client.close()
        assert ok is True
        assert archived == ["closed"]
    finally:
        conn.close()


# ============================================================================
# Jira
# ============================================================================


def jira_response(payload: dict, status: int = 201) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())


def make_jira_client(handler) -> jira.JiraClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        headers={
            "Authorization": "Basic dGVzdEB4OnRva2Vu",  # test@x:token
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "verbatim/test",
        },
    )
    return jira.JiraClient(
        site="https://test.atlassian.net",
        email="test@x.com", api_token="token",
        client=http,
    )


def test_jira_constructor_requires_creds() -> None:
    with pytest.raises(ValueError):
        jira.JiraClient(site="https://x.atlassian.net", email="", api_token="t")
    with pytest.raises(ValueError):
        jira.JiraClient(site="https://x.atlassian.net", email="e", api_token="")
    with pytest.raises(ValueError):
        jira.JiraClient(site="", email="e", api_token="t")


def test_jira_create_issue_returns_payload_with_browse_url() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        captured.append({"path": request.url.path, "body": body})
        return jira_response({
            "id": "10001", "key": "ENG-42",
            "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        })

    client = make_jira_client(handler)
    issue = client.create_issue(
        project_key="ENG",
        summary="Alice: ship v0",
        description_adf=jira.adf_doc([jira.adf_paragraph("hi")]),
        issuetype="Task",
        labels=["verbatim"],
    )
    client.close()
    assert issue["key"] == "ENG-42"
    assert issue["_browse_url"] == "https://test.atlassian.net/browse/ENG-42"
    body = captured[0]["body"]
    assert body["fields"]["project"]["key"] == "ENG"
    assert body["fields"]["summary"] == "Alice: ship v0"
    assert body["fields"]["issuetype"]["name"] == "Task"
    assert "verbatim" in body["fields"]["labels"]


def test_jira_render_uses_adf_blockquote_for_sources(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        entity = store.fetch_entity(conn, eid)
        draft = jira.render_issue_from_commitment(entity)
        adf = draft.description_adf
        assert adf["type"] == "doc"
        assert adf["version"] == 1
        # The verbatim quote should show up inside a blockquote node
        quote_blocks = [b for b in adf["content"] if b.get("type") == "blockquote"]
        assert len(quote_blocks) >= 1
        # And the quote text is present
        joined = json.dumps(adf)
        assert "I'll ship Friday." in joined
        assert eid in joined
    finally:
        conn.close()


def test_jira_plan_skips_non_canonical(tmp_path: Path) -> None:
    db = tmp_path / "j_merged.db"
    conn = state.open_db(db)
    try:
        id_a = _seed(conn, deliverable="ship v0")
        id_b = _seed(conn, deliverable="ship v0 by EOD")
        reconcile.link_entities(conn, canonical_id=id_a, member_id=id_b)
        merged_entity = store.fetch_entity(conn, id_b)
        plan = jira.plan_projection(conn, merged_entity)
        assert plan.skip_reason is not None
        assert "non-canonical" in plan.skip_reason
    finally:
        conn.close()


def test_jira_idempotency_second_plan_skips(tmp_path: Path) -> None:
    db = tmp_path / "j_idem.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        entity = store.fetch_entity(conn, eid)

        def handler(request: httpx.Request) -> httpx.Response:
            return jira_response({
                "id": "100", "key": "ENG-1",
                "self": "https://test.atlassian.net/rest/api/3/issue/100",
            })

        client = make_jira_client(handler)
        plan1 = jira.plan_projection(conn, entity)
        jira.execute_projection(conn, client, plan1, project_key="ENG")
        plan2 = jira.plan_projection(conn, entity)
        client.close()
        assert plan2.skip_reason is not None
        assert "ENG-1" in plan2.skip_reason


    finally:
        conn.close()


def test_jira_close_issue_via_deactivate(tmp_path: Path) -> None:
    db = tmp_path / "j_close.db"
    conn = state.open_db(db)
    try:
        eid = _seed(conn)
        pid = store.insert_projection(
            conn, entity_id=eid, target_kind=jira.TARGET_KIND,
            external_id="100", external_url="https://test.atlassian.net/browse/ENG-1",
            metadata={"key": "ENG-1", "project_key": "ENG"},
        )

        transition_invocations: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/transitions" in request.url.path:
                return jira_response({
                    "transitions": [{"id": "31", "name": "Done"}]
                }, status=200)
            if request.method == "POST" and "/transitions" in request.url.path:
                transition_invocations.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(404)

        client = make_jira_client(handler)
        ok = jira.deactivate_projection(
            conn, pid, client=client, close_external=True,
        )
        client.close()
        assert ok is True
        assert transition_invocations == [{"transition": {"id": "31"}}]
    finally:
        conn.close()


# ----- adf helpers -----


def test_adf_doc_structure() -> None:
    doc = jira.adf_doc([jira.adf_paragraph("hi"), jira.adf_quote("quoted")])
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert len(doc["content"]) == 2
    assert doc["content"][0]["type"] == "paragraph"
    assert doc["content"][1]["type"] == "blockquote"
