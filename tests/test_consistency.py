"""Cross-surface consistency tests.

Pins the contracts that must hold across CLI / web / Slack / email outputs:
- Entity IDs are truncated to 8 chars + ellipsis on every surface
- "merged with N other source(s)" copy is identical across surfaces
- Pluralization is correct (1 source / 2+ sources)
- Activity feed renders on the dashboard
- A11y skip link + aria-current are present
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from verbatim import slack_bot, state, web
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# ----- fixtures -----


def _save_one_commitment(conn: sqlite3.Connection, *, deliverable: str) -> str:
    result = ExtractionResult(
        meeting_summary="seed",
        participants=["Qat"],
        commitments=[Commitment(
            actor="Qat", deliverable=deliverable,
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="x", speaker="Qat", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path=None)
    row = conn.execute(
        "SELECT id FROM entities WHERE primary_actor = 'Qat' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["id"]


@pytest.fixture
def db_with_merged(tmp_path: Path) -> Path:
    """Two commitments where the second is linked into the first."""
    from verbatim import reconcile
    db_path = tmp_path / "consistency.db"
    conn = state.open_db(db_path)
    try:
        id_a = _save_one_commitment(conn, deliverable="ship v0")
        id_b = _save_one_commitment(conn, deliverable="ship v0 by EOD")
        reconcile.link_entities(conn, canonical_id=id_a, member_id=id_b)
    finally:
        conn.close()
    return db_path


@pytest.fixture
def client(db_with_merged: Path) -> TestClient:
    return TestClient(web.create_app(db_path=db_with_merged))


# ----- entity ID truncation contract -----


def test_slack_bot_truncates_ids_to_8_chars() -> None:
    """Slack bot list formatters use [:8] like every other surface."""
    items = [{
        "id": "0123456789abcdef0123456789abcdef",
        "kind": "commitment",
        "confidence": "high",
        "payload": {"actor": "Qat", "deliverable": "x"},
        "sources": [],
        "merged_count": 0,
    }]
    text = slack_bot.format_commitments(items)
    assert "01234567" in text
    assert "0123456789" not in text  # not 10 chars


def test_slack_bot_truncates_decisions_questions_blockers_to_8(seeded_conn=None) -> None:
    for fn, kind in [
        (slack_bot.format_decisions, "decision"),
        (slack_bot.format_questions, "open_question"),
        (slack_bot.format_blockers, "blocker"),
    ]:
        items = [{
            "id": "deadbeef12345678abcdef0123456789",
            "kind": kind,
            "confidence": "high",
            "payload": {"topic": "t", "outcome": "o", "question": "q",
                        "blocked_thing": "x", "blocked_by": "y"},
            "sources": [],
            "merged_count": 0,
        }]
        text = fn(items)
        assert "deadbeef" in text
        assert "deadbeef12" not in text  # not 10 chars


# ----- merged-source language contract -----


def test_web_detail_says_merged_with_n_other_source(client: TestClient, db_with_merged: Path) -> None:
    conn = state.open_db(db_with_merged)
    try:
        items = state.list_commitments(conn)
        canonical_id = items[0]["id"]
    finally:
        conn.close()
    r = client.get(f"/entity/{canonical_id}")
    # merged_count is 1 → "1 other source" (singular)
    assert "merged with 1 other source" in r.text


def test_slack_bot_detail_says_merged_with_n_other_source(db_with_merged: Path) -> None:
    conn = state.open_db(db_with_merged)
    try:
        items = state.list_commitments(conn)
        entity = state.show_entity(conn, items[0]["id"])
    finally:
        conn.close()
    text = slack_bot.format_entity_detail(entity)
    assert "merged with 1 other source" in text


def test_pluralization_single_source(db_with_merged: Path) -> None:
    """N=1 should say 'source' (singular), not 'sources'."""
    conn = state.open_db(db_with_merged)
    try:
        items = state.list_commitments(conn)
        entity = state.show_entity(conn, items[0]["id"])
    finally:
        conn.close()
    text = slack_bot.format_entity_detail(entity)
    assert "1 other source)" in text
    assert "1 other sources" not in text


def test_pluralization_multiple_sources(tmp_path: Path) -> None:
    """N>=2 should say 'sources' (plural)."""
    from verbatim import reconcile
    db_path = tmp_path / "many_merged.db"
    conn = state.open_db(db_path)
    try:
        id_a = _save_one_commitment(conn, deliverable="ship v0")
        id_b = _save_one_commitment(conn, deliverable="ship v0 by EOD")
        id_c = _save_one_commitment(conn, deliverable="ship v0 Friday")
        reconcile.link_entities(conn, canonical_id=id_a, member_id=id_b)
        reconcile.link_entities(conn, canonical_id=id_a, member_id=id_c)
        entity = state.show_entity(conn, id_a)
    finally:
        conn.close()
    text = slack_bot.format_entity_detail(entity)
    assert "2 other sources" in text


def test_web_and_slack_use_same_merged_phrase(db_with_merged: Path) -> None:
    """Both surfaces should agree on the exact wording 'merged with N other source(s)'."""
    conn = state.open_db(db_with_merged)
    try:
        items = state.list_commitments(conn)
        entity = state.show_entity(conn, items[0]["id"])
    finally:
        conn.close()
    slack_text = slack_bot.format_entity_detail(entity)
    # Both should contain this exact substring
    assert "merged with 1 other source" in slack_text
    # Web is tested in test_web_detail_says_merged_with_n_other_source


def test_slack_list_view_shows_compact_merged_pill(db_with_merged: Path) -> None:
    """List views (commitments, decisions etc.) use the compact `+N merged` form."""
    conn = state.open_db(db_with_merged)
    try:
        items = state.list_commitments(conn)
    finally:
        conn.close()
    text = slack_bot.format_commitments(items)
    assert "+1 merged" in text


# ----- activity feed on dashboard -----


def test_dashboard_renders_activity_feed(client: TestClient) -> None:
    r = client.get("/")
    assert "activity-feed" in r.text
    assert "activity-item" in r.text
    assert "Ingested" in r.text  # text from our activity feed renderer


def test_activity_feed_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    state.open_db(db_path).close()
    c = TestClient(web.create_app(db_path=db_path))
    r = c.get("/")
    # No "Activity" section header because there are no sessions
    assert "Activity</h2>" not in r.text
    # The feed renderer itself isn't called when there are no sessions
    # (the page only contains CSS rules mentioning .activity-feed, not the markup)
    assert '<div class="activity-feed">' not in r.text


# ----- a11y -----


def test_skip_link_present(client: TestClient) -> None:
    """Every page should have a skip-to-content link for keyboard users."""
    for path in ("/", "/commitments", "/decisions", "/search?q=x"):
        r = client.get(path)
        assert 'class="skip-link"' in r.text
        assert 'href="#main-content"' in r.text


def test_main_has_skip_target_id(client: TestClient) -> None:
    r = client.get("/")
    assert 'id="main-content"' in r.text


def test_active_nav_link_has_aria_current(client: TestClient) -> None:
    r = client.get("/commitments")
    # The /commitments anchor should have both class="active" and aria-current="page"
    assert 'href="/commitments" class="active" aria-current="page"' in r.text


def test_search_input_has_aria_label(client: TestClient) -> None:
    r = client.get("/")
    assert 'aria-label="Search Verbatim state"' in r.text


def test_nav_has_aria_label(client: TestClient) -> None:
    r = client.get("/")
    assert 'aria-label="Primary"' in r.text
