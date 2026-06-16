"""Tests pinning the v0.8 design contracts.

These tests guard the structural pieces of the Claude Design implementation
(see docs/design/verbatim.html) so future changes don't drift away from
the intentional design.
"""
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
    db_path = tmp_path / "design.db"
    conn = state.open_db(db_path)
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="design seed",
                participants=["Alice", "Bob"],
                commitments=[Commitment(
                    actor="Alice", deliverable="ship the migration",
                    deadline="EOW", confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="I'll have it ready by EOW.",
                        speaker="Alice", rationale="explicit",
                        approximate_timestamp="10:30",
                    )],
                )],
                decisions=[Decision(
                    topic="storage", outcome="Postgres",
                    participants=["Alice"], confidence=Confidence.HIGH,
                    sources=[SourceReference(verbatim_quote="postgres it is.", speaker="Alice", rationale="r")],
                )],
                open_questions=[OpenQuestion(
                    topic="budget", question="What's the API budget?",
                    raised_by="Carol", confidence=Confidence.MEDIUM,
                    sources=[SourceReference(verbatim_quote="any budget?", speaker="Carol", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="public ship", blocked_by="legal review",
                    owner="Carol", confidence=Confidence.LOW,
                    sources=[SourceReference(verbatim_quote="need legal.", speaker="Bob", rationale="r")],
                )],
            ),
            diag, source_path="meeting.txt",
        )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def client(seeded_db: Path) -> TestClient:
    return TestClient(web.create_app(db_path=seeded_db))


# ----- three-pane shell -----


def test_inbox_renders_three_pane_shell(client: TestClient) -> None:
    r = client.get("/")
    body = r.text
    assert 'class="shell"' in body
    assert 'class="sidebar"' in body
    assert 'class="list-pane"' in body or 'list-pane' in body
    assert 'class="detail"' in body


def test_inbox_default_selects_first_item(client: TestClient) -> None:
    """With no ?id= passed, the inbox should auto-select the first item."""
    r = client.get("/")
    # The default selection means a quote-hero card should appear
    assert "quote-hero" in r.text


def test_inbox_respects_id_query_param(client: TestClient, seeded_db: Path) -> None:
    """?id=<entity_id> selects that specific item in the detail pane."""
    conn = state.open_db(seeded_db)
    try:
        items = state.list_commitments(conn)
        target_id = items[0]["id"]
    finally:
        conn.close()
    r = client.get(f"/?id={target_id}")
    # The detail pane should render this entity's quote
    assert "I&#x27;ll have it ready by EOW." in r.text or "I'll have it ready by EOW." in r.text


# ----- quote hero -----


def test_quote_hero_renders_on_inbox_with_selection(client: TestClient) -> None:
    """The violet quote-hero card is the page-defining element when an entity is selected."""
    r = client.get("/")
    body = r.text
    assert 'class="quote-hero"' in body
    assert 'class="quote-hero-label"' in body
    assert 'class="lock">verbatim<' in body  # the "verbatim" lock tag


def test_quote_hero_on_standalone_entity_detail(client: TestClient, seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        items = state.list_commitments(conn)
        eid = items[0]["id"]
    finally:
        conn.close()
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="quote-hero"' in r.text
    assert "I&#x27;ll have it ready by EOW." in r.text or "I'll have it ready by EOW." in r.text


# ----- type dots -----


def test_type_dot_css_variables_defined(client: TestClient) -> None:
    """Each entity kind has its own color in the CSS root."""
    r = client.get("/")
    body = r.text
    for var in ("--commitment", "--decision", "--question", "--blocker"):
        assert var in body
    # And the per-type classes
    for cls in ("type-dot.commitment", "type-dot.decision", "type-dot.open_question", "type-dot.blocker"):
        assert cls in body


def test_filter_tabs_have_type_dots(client: TestClient) -> None:
    """Filter tabs above the list show a colored dot per kind."""
    r = client.get("/")
    body = r.text
    # Each tab has class "filter-tab" + a type-dot
    assert 'class="filter-tab' in body
    assert 'type-dot commitment' in body
    assert 'type-dot decision' in body
    assert 'type-dot question' in body
    assert 'type-dot blocker' in body


# ----- short ID format (VRB-<8>) -----


def test_short_id_format_appears_in_rows(client: TestClient, seeded_db: Path) -> None:
    """Rows use the VRB-<short id> format."""
    conn = state.open_db(seeded_db)
    try:
        items = state.list_commitments(conn)
        eid_prefix = items[0]["id"][:8]
    finally:
        conn.close()
    r = client.get("/commitments")
    assert f"VRB-{eid_prefix}" in r.text


# ----- light + dark theme -----


def test_theme_toggle_button_present(client: TestClient) -> None:
    """A theme toggle button is in the sidebar footer."""
    r = client.get("/")
    body = r.text
    assert 'id="theme-toggle"' in body
    # Both icons are in the markup; CSS handles which one shows
    assert 'data-icon="moon"' in body
    assert 'data-icon="sun"' in body


def test_light_theme_css_tokens_present(client: TestClient) -> None:
    """The [data-theme='light'] rules ship with the page."""
    r = client.get("/")
    assert '[data-theme="light"]' in r.text


def test_theme_persistence_script_present(client: TestClient) -> None:
    """The early-load script reads localStorage for theme before paint."""
    r = client.get("/")
    body = r.text
    assert "localStorage.getItem('verbatim-theme')" in body or \
           "localStorage.getItem(\"verbatim-theme\")" in body


# ----- typography -----


def test_inter_and_jbmono_fonts_loaded(client: TestClient) -> None:
    r = client.get("/")
    body = r.text
    assert "fonts.googleapis.com" in body
    assert "Inter" in body
    assert "JetBrains+Mono" in body or "JetBrains Mono" in body


# ----- sidebar polish -----


def test_brand_uses_lowercase_wordmark(client: TestClient) -> None:
    """Per design: lowercase 'verbatim' next to the icon (dev-tool aesthetic)."""
    r = client.get("/")
    assert "brand-word" in r.text
    assert ">verbatim<" in r.text


def test_sidebar_has_ingest_pulse(client: TestClient) -> None:
    """The animated green dot is in the sidebar footer."""
    r = client.get("/")
    assert "ingest-pulse" in r.text


def test_sidebar_search_form_action_is_search(client: TestClient) -> None:
    r = client.get("/")
    assert 'action="/search"' in r.text


# ----- detail pane structure -----


def test_detail_breadcrumb_present(client: TestClient) -> None:
    """Detail pane has a breadcrumb (Inbox / Commitments / VRB-...)."""
    r = client.get("/commitments")
    body = r.text
    assert 'class="breadcrumb"' in body
    assert ">Inbox<" in body


def test_detail_right_rail_has_properties_and_evidence(client: TestClient) -> None:
    r = client.get("/")
    body = r.text
    assert ">Properties<" in body
    assert ">Evidence<" in body
    # Confidence bar is rendered as a div with width %
    assert "confidence-bar" in body


# ----- responsive -----


def test_viewport_meta_present(client: TestClient) -> None:
    r = client.get("/")
    assert 'name="viewport"' in r.text


# ----- legacy dashboard still works -----


def test_dashboard_route_renders_with_stats(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Verbatim dashboard" in r.text
    assert "stat-grid" in r.text
