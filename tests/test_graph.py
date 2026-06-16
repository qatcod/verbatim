"""Tests for v0.12.2 — the visual relationship graph (verbatim.graph) and
the /graph web route."""
from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from verbatim import graph, state, store, web
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
    conn = state.open_db(db_path)
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Alice"],
                commitments=[Commitment(
                    actor="Alice", deliverable="run the security review",
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
                decisions=[Decision(
                    topic="db", outcome="Postgres", participants=["Alice"],
                    confidence=Confidence.HIGH,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
                open_questions=[OpenQuestion(
                    topic="db", question="which database?", raised_by="Alice",
                    confidence=Confidence.MEDIUM,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
                blockers=[Blocker(
                    blocked_thing="launch", blocked_by="security review",
                    owner="Alice", confidence=Confidence.LOW,
                    sources=[SourceReference(
                        verbatim_quote="q", speaker="Alice", rationale="r")],
                )],
            ),
            _diag(), source_path="m.txt",
        )
        ids = {}
        for kind in ("commitment", "decision", "open_question", "blocker"):
            ids[kind] = conn.execute(
                "SELECT id FROM entities WHERE kind = ?", (kind,)
            ).fetchone()["id"]
        return ids
    finally:
        conn.close()


# ----- build_graph -----


def test_build_graph_empty_when_no_relationships(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    _seed(db)
    conn = state.open_db(db)
    try:
        g = graph.build_graph(conn)
    finally:
        conn.close()
    assert g.is_empty
    assert g.nodes == []
    assert g.edges == []


def test_build_graph_includes_related_entities(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        g = graph.build_graph(conn)
    finally:
        conn.close()
    assert not g.is_empty
    assert len(g.nodes) == 2
    assert len(g.edges) == 1
    assert g.edges[0].rel_type == "resolves"


def test_build_graph_excludes_unrelated_entities(tmp_path: Path) -> None:
    """An entity with no relationships is not a node."""
    db = tmp_path / "g.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="answers",
        )
        g = graph.build_graph(conn)
    finally:
        conn.close()
    node_ids = {n.entity_id for n in g.nodes}
    assert ids["decision"] in node_ids
    assert ids["open_question"] in node_ids
    assert ids["commitment"] not in node_ids
    assert ids["blocker"] not in node_ids


def test_build_graph_nodes_carry_kind_and_label(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        g = graph.build_graph(conn)
    finally:
        conn.close()
    by_id = {n.entity_id: n for n in g.nodes}
    assert by_id[ids["commitment"]].kind == "commitment"
    assert "security review" in by_id[ids["commitment"]].label


def test_layout_assigns_distinct_positions(tmp_path: Path) -> None:
    """The force layout must spread nodes — not stack them on one point."""
    db = tmp_path / "g.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        store.add_relationship(
            conn, from_entity_id=ids["decision"],
            to_entity_id=ids["open_question"], rel_type="answers",
        )
        g = graph.build_graph(conn)
    finally:
        conn.close()
    points = {(round(n.x), round(n.y)) for n in g.nodes}
    assert len(points) == len(g.nodes)  # all distinct
    for n in g.nodes:
        assert 0 <= n.x <= 1000
        assert 0 <= n.y <= 640


def test_layout_is_deterministic(tmp_path: Path) -> None:
    """Same graph lays out the same way across calls (stable reloads)."""
    db = tmp_path / "g.db"
    ids = _seed(db)
    conn = state.open_db(db)
    try:
        store.add_relationship(
            conn, from_entity_id=ids["commitment"],
            to_entity_id=ids["blocker"], rel_type="resolves",
        )
        g1 = graph.build_graph(conn)
        g2 = graph.build_graph(conn)
    finally:
        conn.close()
    p1 = [(round(n.x, 3), round(n.y, 3)) for n in g1.nodes]
    p2 = [(round(n.x, 3), round(n.y, 3)) for n in g2.nodes]
    assert p1 == p2


# ----- /graph web route -----


def test_graph_route_empty_state(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    _seed(db)
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/graph")
    assert r.status_code == 200
    assert "No relationships yet" in r.text


def test_graph_route_renders_svg(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
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
    r = client.get("/graph")
    assert r.status_code == 200
    assert "<svg" in r.text
    assert "<circle" in r.text
    assert "resolves" in r.text
    # Nodes link through to their entity pages.
    assert f'href="/entity/{ids["commitment"]}"' in r.text


def test_graph_link_in_sidebar(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    _seed(db)
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/")
    assert 'href="/graph"' in r.text
