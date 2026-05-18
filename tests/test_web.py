"""Web UI tests — every route renders, filters work, escaping is safe."""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from verbatim import state, store, web
from verbatim.extractor import ExtractionDiagnostics
from verbatim.projections import linear as linear_proj
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A DB pre-populated with one of each entity kind."""
    db_path = tmp_path / "test_web.db"
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="web seed",
            participants=["Qat", "Jason"],
            commitments=[Commitment(
                actor="Qat", deliverable="ship v0", deadline="Friday",
                confidence=Confidence.HIGH,
                sources=[SourceReference(
                    verbatim_quote="I'll ship Friday.", speaker="Qat",
                    rationale="r", approximate_timestamp="10:30",
                )],
            )],
            decisions=[Decision(
                topic="language", outcome="Python",
                participants=["Qat"], confidence=Confidence.HIGH,
                sources=[SourceReference(verbatim_quote="python.", speaker="Qat", rationale="r")],
            )],
            open_questions=[OpenQuestion(
                topic="cost", question="What's the budget?",
                raised_by="Taz", confidence=Confidence.MEDIUM,
                sources=[SourceReference(verbatim_quote="budget?", speaker="Taz", rationale="r")],
            )],
            blockers=[Blocker(
                blocked_thing="ship public", blocked_by="review",
                owner="Taz", confidence=Confidence.LOW,
                sources=[SourceReference(verbatim_quote="not yet.", speaker="Jason", rationale="r")],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="meeting.txt")
    finally:
        conn.close()
    return db_path


@pytest.fixture
def client(seeded_db: Path) -> TestClient:
    return TestClient(web.create_app(db_path=seeded_db))


# ----- dashboard -----


def test_home_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Verbatim dashboard" in body
    assert "<title>Dashboard · Verbatim</title>" in body
    assert "Qat" in body  # recent commitment surfaces


def test_home_stats_present(client: TestClient) -> None:
    r = client.get("/")
    body = r.text
    for label in ("Sessions", "Commitments", "Decisions", "Questions", "Blockers"):
        assert label in body


# ----- list pages -----


def test_commitments_page(client: TestClient) -> None:
    r = client.get("/commitments")
    assert r.status_code == 200
    assert "ship v0" in r.text
    assert "Friday" in r.text


def test_commitments_filter_by_actor(client: TestClient) -> None:
    r_qat = client.get("/commitments?actor=qat")
    r_none = client.get("/commitments?actor=unknown")
    assert "ship v0" in r_qat.text
    assert "ship v0" not in r_none.text
    assert "No commitments match" in r_none.text


def test_commitments_filter_by_min_confidence(client: TestClient) -> None:
    r_high = client.get("/commitments?min_confidence=high")
    r_only_low = client.get("/commitments?min_confidence=low")
    # Only one commitment, high confidence — both should return it
    assert "ship v0" in r_high.text
    assert "ship v0" in r_only_low.text


def test_decisions_page(client: TestClient) -> None:
    r = client.get("/decisions")
    assert r.status_code == 200
    assert "language" in r.text
    assert "Python" in r.text


def test_open_questions_page(client: TestClient) -> None:
    r = client.get("/open-questions")
    assert r.status_code == 200
    assert "What&#x27;s the budget?" in r.text or "What's the budget?" in r.text
    assert "Taz" in r.text


def test_blockers_page(client: TestClient) -> None:
    r = client.get("/blockers")
    assert r.status_code == 200
    assert "ship public" in r.text
    assert "review" in r.text


def test_sessions_page(client: TestClient) -> None:
    r = client.get("/sessions")
    assert r.status_code == 200
    assert "meeting.txt" in r.text


def test_projections_page_empty(client: TestClient) -> None:
    r = client.get("/projections")
    assert r.status_code == 200
    assert "No active projections" in r.text


def test_projections_page_with_one(client: TestClient, seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        commits = state.list_commitments(conn)
        store.insert_projection(
            conn, entity_id=commits[0]["id"], target_kind=linear_proj.TARGET_KIND,
            external_id="issue-1", external_url="https://linear.app/x/ENG-1",
            metadata={"identifier": "ENG-1"},
        )
    finally:
        conn.close()

    r = client.get("/projections")
    assert "ENG-1" in r.text
    assert "linear.app" in r.text


# ----- entity detail -----


def test_entity_detail(client: TestClient, seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        commits = state.list_commitments(conn)
        entity_id = commits[0]["id"]
    finally:
        conn.close()
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    assert "ship v0" in r.text
    assert "I&#x27;ll ship Friday." in r.text or "I'll ship Friday." in r.text
    assert entity_id in r.text


def test_entity_detail_404(client: TestClient) -> None:
    r = client.get("/entity/deadbeef-not-real")
    assert r.status_code == 404
    assert "Entity not found" in r.text


# ----- escaping -----


def test_html_escapes_user_content(tmp_path: Path) -> None:
    """A malicious actor/topic should not break out into raw HTML."""
    db_path = tmp_path / "xss.db"
    conn = state.open_db(db_path)
    try:
        result = ExtractionResult(
            meeting_summary="<script>alert(1)</script>",
            participants=[],
            commitments=[Commitment(
                actor="<img src=x onerror=alert(1)>",
                deliverable="</td><script>alert(1)</script>",
                confidence=Confidence.HIGH,
                sources=[SourceReference(
                    verbatim_quote="<b>bold</b>", speaker="<i>x</i>", rationale="r",
                )],
            )],
        )
        diag = ExtractionDiagnostics(
            model="t", input_tokens=1, output_tokens=1,
            stop_reason="end_turn", transcript_chars=10,
        )
        state.save_extraction(conn, result, diag, source_path="x")
    finally:
        conn.close()

    c = TestClient(web.create_app(db_path=db_path))
    r = c.get("/commitments")
    assert r.status_code == 200
    body = r.text
    # No unescaped <script> from the user-supplied actor/deliverable
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    # Even the quote in the entity detail should be escaped
    eid_row = conn = state.open_db(db_path)
    items = state.list_commitments(eid_row)
    eid = items[0]["id"]
    eid_row.close()
    r2 = c.get(f"/entity/{eid}")
    assert "<b>bold</b>" not in r2.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in r2.text


# ----- navigation -----


def test_nav_links_present_on_every_page(client: TestClient) -> None:
    for path in ("/", "/commitments", "/decisions", "/open-questions",
                 "/blockers", "/sessions", "/projections"):
        r = client.get(path)
        body = r.text
        for nav_path, label in web._NAV_LINKS:
            assert f'href="{nav_path}"' in body
            assert label in body


def test_active_nav_class_on_current_page(client: TestClient) -> None:
    r = client.get("/commitments")
    body = r.text
    # The /commitments link should have class="active"
    assert 'href="/commitments"' in body
    assert 'class="active">Commitments' in body
