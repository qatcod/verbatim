"""Cross-entity search tests — state.search + the /search route."""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from verbatim import state, web
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


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A DB with a mix of entity kinds suitable for testing search."""
    db_path = tmp_path / "search.db"
    conn = state.open_db(db_path)
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed",
                participants=["Alice", "Bob"],
                commitments=[Commitment(
                    actor="Alice", deliverable="ship the Xero pilot by Friday",
                    deadline="Friday", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="I'll ship the Xero pilot by Friday.",
                        speaker="Alice", rationale="r",
                    )],
                )],
                decisions=[Decision(
                    topic="database choice", outcome="Postgres",
                    participants=["Alice", "Bob"],
                    rationale="multi-writer concerns with SQLite",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="Postgres, definitely.", speaker="Bob",
                        rationale="r",
                    )],
                )],
                open_questions=[OpenQuestion(
                    topic="staffing", question="Who reviews the Xero pull request?",
                    raised_by="Carol", confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="who's reviewing this?", speaker="Carol",
                        rationale="r",
                    )],
                )],
                blockers=[Blocker(
                    blocked_thing="public launch",
                    blocked_by="security review",
                    owner="Alice", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="security needs to clear it first.",
                        speaker="Bob", rationale="r",
                    )],
                )],
            ),
            diag, source_path="m.txt",
        )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def client(seeded_db: Path) -> TestClient:
    return TestClient(web.create_app(db_path=seeded_db))


# ----- state.search backend -----


def test_search_matches_commitment_by_actor(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "alice")
    finally:
        conn.close()
    assert len(out["commitment"]) == 1
    assert out["commitment"][0]["primary_actor"] == "Alice"


def test_search_matches_decision_by_topic(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "database")
    finally:
        conn.close()
    assert len(out["decision"]) == 1
    assert "database" in out["decision"][0]["primary_topic"].lower()


def test_search_matches_question_by_topic(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "staffing")
    finally:
        conn.close()
    assert len(out["open_question"]) == 1


def test_search_matches_payload_field(seeded_db: Path) -> None:
    """Searching 'SQLite' should hit the decision's rationale (in payload_json)."""
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "SQLite")
    finally:
        conn.close()
    assert len(out["decision"]) == 1


def test_search_quote_only_match(seeded_db: Path) -> None:
    """A query that only appears in a verbatim quote, not in any direct field."""
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "clear it first")
    finally:
        conn.close()
    # The blocker's verbatim quote contains this; direct fields don't
    assert len(out["source_match"]) == 1
    assert out["source_match"][0]["kind"] == "blocker"


def test_search_deduplicates_direct_and_quote_matches(seeded_db: Path) -> None:
    """An entity matched directly should NOT also appear in source_match."""
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "Xero")
    finally:
        conn.close()
    direct_ids = {e["id"] for e in out["commitment"]}
    source_ids = {e["id"] for e in out["source_match"]}
    assert not (direct_ids & source_ids)


def test_search_empty_query_returns_empty_groups(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        out = state.search(conn, "")
    finally:
        conn.close()
    assert all(v == [] for v in out.values())


def test_search_case_insensitive(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        upper = state.search(conn, "XERO")
        lower = state.search(conn, "xero")
    finally:
        conn.close()
    assert len(upper["commitment"]) == len(lower["commitment"])
    assert len(upper["commitment"]) == 1


# ----- /search route -----


def test_search_route_empty_query(client: TestClient) -> None:
    r = client.get("/search")
    assert r.status_code == 200
    assert "Type something" in r.text


def test_search_route_no_results(client: TestClient) -> None:
    r = client.get("/search?q=nonexistent-string")
    assert r.status_code == 200
    assert "No matches" in r.text


def test_search_route_renders_matches(client: TestClient) -> None:
    r = client.get("/search?q=Xero")
    assert r.status_code == 200
    body = r.text
    assert 'Search: &quot;Xero&quot;' in body or 'Search: "Xero"' in body
    assert "Commitments" in body  # group header
    assert "ship the Xero pilot" in body or "ship the " in body


def test_search_route_highlights_match(client: TestClient) -> None:
    r = client.get("/search?q=Friday")
    assert "<mark>Friday</mark>" in r.text


def test_search_box_in_sidebar(client: TestClient) -> None:
    """Every page should include the sidebar search input."""
    for path in ("/", "/commitments", "/decisions", "/sessions"):
        r = client.get(path)
        assert 'id="sidebar-search-input"' in r.text
        assert 'action="/search"' in r.text


def test_search_box_preserves_query_on_results_page(client: TestClient) -> None:
    r = client.get("/search?q=Postgres")
    # The query should be back-filled into the search input as its value
    assert 'value="Postgres"' in r.text


def test_keyboard_shortcut_script_present(client: TestClient) -> None:
    """The `/` shortcut must be wired in the shell on every page."""
    r = client.get("/")
    assert "sidebar-search-input" in r.text
    assert "e.key === '/'" in r.text


# ----- highlight() -----


def test_highlight_wraps_match() -> None:
    out = web._highlight("Ship the Xero pilot", "xero")
    assert "<mark>Xero</mark>" in out


def test_highlight_multiple_matches() -> None:
    out = web._highlight("foo bar foo baz", "foo")
    assert out.count("<mark>foo</mark>") == 2


def test_highlight_escapes_html_in_unmatched_parts() -> None:
    out = web._highlight("<script>alert(1)</script> hello", "hello")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_highlight_returns_escaped_empty_for_empty_text() -> None:
    assert web._highlight("", "anything") == ""


def test_highlight_returns_escaped_text_for_empty_query() -> None:
    assert web._highlight("hi <there>", "") == "hi &lt;there&gt;"


# ----- new sidebar logo present -----


def test_new_logo_is_rendered(client: TestClient) -> None:
    r = client.get("/")
    # The new logo uses a <g transform="rotate(...)"> wrapping rects.
    assert 'aria-label="Verbatim"' in r.text
    assert 'transform="rotate(-12 16 16)"' in r.text
