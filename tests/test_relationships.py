"""Tests for v0.12.1 — typed entity relationships (store.add/remove/fetch_
relationship) and their CLI + web surfaces."""
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


def _diag() -> ExtractionDiagnostics:
    return ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )


def _seed(db_path: Path) -> dict[str, str]:
    """Seed one of each kind; return {kind: entity_id}."""
    conn = state.open_db(db_path)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[Commitment(
                    actor="Qat", deliverable="run the security review",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="I'll run it.", speaker="Qat", rationale="r")],
                )],
                decisions=[Decision(
                    topic="db", outcome="Postgres", participants=["Qat"],
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="Postgres.", speaker="Qat", rationale="r")],
                )],
                open_questions=[OpenQuestion(
                    topic="db", question="which database?", raised_by="Qat",
                    confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="which db?", speaker="Qat", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="launch", blocked_by="security review",
                    owner="Qat", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="security first.", speaker="Qat", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        ids = {}
        for kind in ("commitment", "decision", "open_question", "blocker"):
            row = conn.execute(
                "SELECT id FROM entities WHERE kind = ?", (kind,)
            ).fetchone()
            ids[kind] = row["id"]
        return ids
    finally:
        conn.close()


# ----- add_relationship -----


def test_add_relationship_creates_edge(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        rel_id = store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        rels = store.fetch_relationships(conn, ids["commitment"])
    finally:
        conn.close()
    assert rel_id
    assert len(rels["outgoing"]) == 1
    assert rels["outgoing"][0]["rel_type"] == "resolves"
    assert rels["outgoing"][0]["entity"]["id"] == ids["blocker"]


def test_relationship_appears_incoming_on_target(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        rels = store.fetch_relationships(conn, ids["blocker"])
    finally:
        conn.close()
    assert len(rels["incoming"]) == 1
    assert rels["incoming"][0]["entity"]["id"] == ids["commitment"]


def test_add_relationship_rejects_unknown_type(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        with pytest.raises(store.RelationshipError, match="Unknown relationship type"):
            store.add_relationship(
                conn, from_entity_id=ids["commitment"],
                to_entity_id=ids["blocker"], rel_type="frobnicates",
            )
    finally:
        conn.close()


def test_add_relationship_rejects_self_link(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        with pytest.raises(store.RelationshipError, match="itself"):
            store.add_relationship(
                conn, from_entity_id=ids["commitment"],
                to_entity_id=ids["commitment"], rel_type="relates-to",
            )
    finally:
        conn.close()


def test_add_relationship_rejects_missing_entity(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        with pytest.raises(store.RelationshipError, match="not found"):
            store.add_relationship(
                conn, from_entity_id=ids["commitment"],
                to_entity_id="deadbeef" * 4, rel_type="relates-to",
            )
    finally:
        conn.close()


def test_add_relationship_rejects_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="answers",
        )
        with pytest.raises(store.RelationshipError, match="already exists"):
            store.add_relationship(
                conn, from_entity_id=ids["decision"],
                to_entity_id=ids["open_question"], rel_type="answers",
            )
    finally:
        conn.close()


def test_same_pair_different_types_allowed(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="answers",
        )
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="relates-to",
        )
        rels = store.fetch_relationships(conn, ids["decision"])
    finally:
        conn.close()
    assert len(rels["outgoing"]) == 2


# ----- remove_relationship -----


def test_remove_relationship_by_type(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        removed = store.remove_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        rels = store.fetch_relationships(conn, ids["commitment"])
    finally:
        conn.close()
    assert removed == 1
    assert rels["outgoing"] == []


def test_remove_relationship_all_types(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="answers",
        )
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="relates-to",
        )
        removed = store.remove_relationship(
            conn, from_entity_id=ids["decision"], to_entity_id=ids["open_question"],
        )
    finally:
        conn.close()
    assert removed == 2


def test_relationship_cascades_on_entity_delete(tmp_path: Path) -> None:
    """Deleting an entity removes its relationship rows (FK cascade)."""
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        conn.execute("DELETE FROM entities WHERE id = ?", (ids["blocker"],))
        rels = store.fetch_relationships(conn, ids["commitment"])
    finally:
        conn.close()
    assert rels["outgoing"] == []


# ----- web surface -----


def test_entity_detail_renders_relationships(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
    finally:
        conn.close()
    client = TestClient(web.create_app(db_path=db))
    r = client.get(f"/entity/{ids['commitment']}")
    assert r.status_code == 200
    assert "Related items" in r.text
    assert "resolves" in r.text


def test_entity_detail_no_relationship_block_when_none(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    ids = _seed(db)
    client = TestClient(web.create_app(db_path=db))
    r = client.get(f"/entity/{ids['decision']}")
    assert r.status_code == 200
    assert "Related items" not in r.text
