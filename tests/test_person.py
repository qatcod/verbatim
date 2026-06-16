"""Tests for the person view — store.fetch_person, store.list_known_people,
the /people and /person/<name> web routes."""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from verbatim import state, store, web
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


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A DB with multi-actor multi-kind data suitable for person aggregation."""
    db_path = tmp_path / "person.db"
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
                participants=["Alice", "Bob", "Carol"],
                commitments=[
                    Commitment(
                        actor="Alice", deliverable="ship v0", deadline="Friday",
                        confidence=Confidence.HIGH,
                        sources=[SourceReference(
                            verbatim_quote="I'll ship v0 Friday.",
                            speaker="Alice", rationale="r",
                        )],
                    ),
                    Commitment(
                        actor="Bob", deliverable="review v0",
                        confidence=Confidence.MEDIUM,
                        sources=[SourceReference(
                            verbatim_quote="I'll review.",
                            speaker="Bob", rationale="r",
                        )],
                    ),
                ],
                decisions=[Decision(
                    topic="db", outcome="Postgres",
                    participants=["Alice", "Bob"],
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="Postgres.", speaker="Bob", rationale="r",
                    )],
                )],
                open_questions=[OpenQuestion(
                    topic="staffing", question="Who runs ops?",
                    raised_by="Carol", confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="Who runs ops?",
                        speaker="Carol", rationale="r",
                    )],
                )],
                blockers=[Blocker(
                    blocked_thing="launch", blocked_by="security review",
                    owner="Alice", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="security first.",
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


# ----- store.fetch_person -----


def test_fetch_person_aggregates_all_four_kinds(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        view = store.fetch_person(conn, "Alice")
    finally:
        conn.close()
    assert view["name"] == "Alice"
    assert view["stats"]["commitments"] == 1
    assert view["stats"]["decisions"] == 1
    assert view["stats"]["blockers_owned"] == 1
    assert view["stats"]["questions_raised"] == 0
    assert view["stats"]["total"] == 3


def test_fetch_person_case_insensitive(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        upper = store.fetch_person(conn, "ALICE")
        lower = store.fetch_person(conn, "alice")
    finally:
        conn.close()
    assert upper["stats"]["total"] == lower["stats"]["total"] == 3


def test_fetch_person_substring_match(seeded_db: Path) -> None:
    """A name fragment should match the full name."""
    conn = state.open_db(seeded_db)
    try:
        view = store.fetch_person(conn, "Bo")  # matches "Bob"
    finally:
        conn.close()
    assert view["stats"]["commitments"] == 1
    assert view["stats"]["decisions"] == 1


def test_fetch_person_unknown_returns_zero(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        view = store.fetch_person(conn, "Nobody")
    finally:
        conn.close()
    assert view["stats"]["total"] == 0
    assert view["commitments"] == []
    assert view["decisions"] == []
    assert view["questions_raised"] == []
    assert view["blockers_owned"] == []


def test_fetch_person_excludes_resolved_by_default(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        # Resolve Alice's commitment
        row = conn.execute(
            "SELECT id FROM entities WHERE kind='commitment' AND primary_actor='Alice'"
        ).fetchone()
        store.update_entity_status(conn, row["id"], "resolved")
        without_resolved = store.fetch_person(conn, "Alice")
        with_resolved = store.fetch_person(conn, "Alice", include_resolved=True)
    finally:
        conn.close()
    assert without_resolved["stats"]["commitments"] == 0
    assert with_resolved["stats"]["commitments"] == 1


def test_fetch_person_decisions_use_participants(seeded_db: Path) -> None:
    """Decisions don't have primary_actor — must come from JSON1 participants."""
    conn = state.open_db(seeded_db)
    try:
        view = store.fetch_person(conn, "Bob")
    finally:
        conn.close()
    assert view["stats"]["decisions"] == 1
    decision = view["decisions"][0]
    assert "Bob" in decision["payload"]["participants"]


# ----- store.list_known_people -----


def test_list_known_people_returns_distinct_names(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        people = store.list_known_people(conn)
    finally:
        conn.close()
    names = {p["name"] for p in people}
    assert "Alice" in names
    assert "Bob" in names
    assert "Carol" in names


def test_list_known_people_sorted_by_frequency(seeded_db: Path) -> None:
    conn = state.open_db(seeded_db)
    try:
        people = store.list_known_people(conn)
    finally:
        conn.close()
    # Alice appears as commitment.actor + blocker.owner = 2 entities;
    # others appear once.
    assert people[0]["name"] == "Alice"
    assert people[0]["total"] == 2


def test_list_known_people_folds_case_variants(tmp_path: Path) -> None:
    """Mixed-case duplicates of the same name fold into a single entry."""
    db_path = tmp_path / "case.db"
    conn = state.open_db(db_path)
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    try:
        for actor in ("Alice", "ALICE", "alice"):
            state.save_extraction(
                conn,
                ExtractionResult(
                    meeting_summary="x", participants=[actor],
                    commitments=[Commitment(
                        actor=actor, deliverable=f"x-{actor}",
                        confidence=Confidence.HIGH,
                        sources=[SourceReference(
                            verbatim_quote=f"q-{actor}",
                            speaker=actor, rationale="r",
                        )],
                    )],
                ),
                diag, source_path=f"m-{actor}.txt",
            )
        people = store.list_known_people(conn)
    finally:
        conn.close()
    qat_entries = [p for p in people if p["name"].lower() == "alice"]
    assert len(qat_entries) == 1
    assert qat_entries[0]["total"] == 3


# ----- /people route -----


def test_people_route_renders(client: TestClient) -> None:
    r = client.get("/people")
    assert r.status_code == 200
    body = r.text
    assert "Alice" in body
    assert "Bob" in body
    assert "Carol" in body


def test_people_route_links_to_person_detail(client: TestClient) -> None:
    r = client.get("/people")
    assert 'href="/person/Alice"' in r.text


def test_people_route_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    # initialize the schema without inserting anything
    state.open_db(db_path).close()
    c = TestClient(web.create_app(db_path=db_path))
    r = c.get("/people")
    assert r.status_code == 200
    assert "No people yet" in r.text


# ----- /person/<name> route -----


def test_person_detail_renders(client: TestClient) -> None:
    r = client.get("/person/Alice")
    assert r.status_code == 200
    body = r.text
    assert "Alice" in body
    assert "Commitments owed" in body
    assert "Blockers owned" in body
    assert "Decisions involved" in body
    # No questions raised by Alice in the seed data
    assert "Questions raised" not in body


def test_person_detail_unknown_name_renders_empty(client: TestClient) -> None:
    r = client.get("/person/Nobody")
    assert r.status_code == 200
    assert "no items" in r.text.lower()
    assert "/people" in r.text


def test_person_detail_html_escapes_name(client: TestClient) -> None:
    """A malicious name reaching the renderer must not break the page."""
    # `<script>` URL-encoded; default str path converter doesn't allow raw `/`.
    r = client.get("/person/%3Cscript%3Ealert(1)%3C")
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text
    assert "&lt;script&gt;" in r.text


# ----- sidebar nav -----


def test_people_link_in_sidebar(client: TestClient) -> None:
    r = client.get("/")
    assert 'href="/people"' in r.text
    assert ">People<" in r.text
