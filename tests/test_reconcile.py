"""Reconciliation engine tests — similarity, candidate selection, link/unlink semantics."""
from __future__ import annotations

import sqlite3

import pytest

from verbatim import reconcile, state, store
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# ----- similarity scoring -----


def test_topic_similarity_identical() -> None:
    assert reconcile.topic_similarity("ship v0 by friday", "ship v0 by friday") == 100


def test_topic_similarity_handles_case_and_order() -> None:
    score = reconcile.topic_similarity("ship v0 by Friday", "by friday ship v0")
    assert score >= 90  # token-set ignores order


def test_topic_similarity_low_for_unrelated() -> None:
    score = reconcile.topic_similarity("write the design doc", "fix the prod outage")
    assert score < 70


def test_topic_similarity_empty_returns_zero() -> None:
    assert reconcile.topic_similarity("", "anything") == 0
    assert reconcile.topic_similarity("anything", "") == 0


# ----- helper to seed canonical entities -----


def _seed_commitment(
    conn: sqlite3.Connection,
    *,
    actor: str,
    deliverable: str,
    confidence: Confidence = Confidence.HIGH,
    quote: str = "I'll do it.",
) -> str:
    """Save a single commitment via state.save_extraction; return its id."""
    result = ExtractionResult(
        meeting_summary="seed",
        participants=[actor],
        commitments=[
            Commitment(
                actor=actor, deliverable=deliverable, confidence=confidence,
                sources=[SourceReference(verbatim_quote=quote, speaker=actor, rationale="test")],
            )
        ],
    )
    diag = ExtractionDiagnostics(
        model="test", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=100,
    )
    state.save_extraction(conn, result, diag, source_path=None)
    # Find the just-saved entity (only commitment in the DB if this is the first call,
    # otherwise the most recently created).
    rows = conn.execute(
        "SELECT id FROM entities WHERE kind = 'commitment' AND primary_actor = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (actor,),
    ).fetchall()
    return rows[0]["id"]


# ----- candidate finding -----


def test_find_candidates_same_actor_similar_topic(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by friday EOD")
    e2 = store.fetch_entity(conn, id2)
    matches = reconcile.find_candidates(conn, e2)
    assert len(matches) == 1
    assert matches[0].candidate["id"] == id1


def test_find_candidates_skips_different_actor(conn: sqlite3.Connection) -> None:
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Jason", deliverable="ship v0 by Friday")
    e2 = store.fetch_entity(conn, id2)
    matches = reconcile.find_candidates(conn, e2)
    assert matches == []


def test_find_candidates_skips_low_similarity(conn: sqlite3.Connection) -> None:
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="write the customer-facing blog post")
    e2 = store.fetch_entity(conn, id2)
    matches = reconcile.find_candidates(conn, e2, threshold=88)
    assert matches == []


def test_find_candidates_threshold_is_respected(conn: sqlite3.Connection) -> None:
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 next week")
    e2 = store.fetch_entity(conn, id2)
    # at 100 threshold no match; at low threshold yes
    assert reconcile.find_candidates(conn, e2, threshold=100) == []
    matches = reconcile.find_candidates(conn, e2, threshold=50)
    assert len(matches) == 1


# ----- link / unlink -----


def test_link_entities_sets_canonical(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday EOD")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    e2 = store.fetch_entity(conn, id2)
    assert e2["canonical_id"] == id1
    assert e2["merged_at"] is not None


def test_link_entities_rejects_self_link(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="x")
    with pytest.raises(ValueError):
        reconcile.link_entities(conn, canonical_id=id1, member_id=id1)


def test_link_entities_resolves_chain_to_root(conn: sqlite3.Connection) -> None:
    """Linking C into B where B is already linked into A must put C under A."""
    id_a = _seed_commitment(conn, actor="Qat", deliverable="a")
    id_b = _seed_commitment(conn, actor="Qat", deliverable="b")
    id_c = _seed_commitment(conn, actor="Qat", deliverable="c")
    reconcile.link_entities(conn, canonical_id=id_a, member_id=id_b)
    reconcile.link_entities(conn, canonical_id=id_b, member_id=id_c)
    # C should point to A (the root), not B
    e_c = store.fetch_entity(conn, id_c)
    assert e_c["canonical_id"] == id_a


def test_link_re_roots_existing_children(conn: sqlite3.Connection) -> None:
    """If A is a canonical with child A', linking A under B makes A' also link to B."""
    id_a = _seed_commitment(conn, actor="Qat", deliverable="a")
    id_a_prime = _seed_commitment(conn, actor="Qat", deliverable="a-prime")
    id_b = _seed_commitment(conn, actor="Qat", deliverable="b")
    reconcile.link_entities(conn, canonical_id=id_a, member_id=id_a_prime)
    # Now link A under B
    reconcile.link_entities(conn, canonical_id=id_b, member_id=id_a)
    e_a = store.fetch_entity(conn, id_a)
    e_a_prime = store.fetch_entity(conn, id_a_prime)
    assert e_a["canonical_id"] == id_b
    assert e_a_prime["canonical_id"] == id_b


def test_unlink_restores_canonical(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday EOD")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    assert reconcile.unlink_entity(conn, id2) is True
    e2 = store.fetch_entity(conn, id2)
    assert e2["canonical_id"] is None
    assert e2["merged_at"] is None


def test_unlink_already_canonical_is_noop(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="x")
    assert reconcile.unlink_entity(conn, id1) is False


# ----- batch reconciliation -----


def test_reconcile_all_links_obvious_duplicates(conn: sqlite3.Connection) -> None:
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday EOD")
    _seed_commitment(conn, actor="Qat", deliverable="completely unrelated thing")
    result = reconcile.reconcile_all(conn)
    assert result.linked == 1
    assert result.no_match >= 1


def test_reconcile_one_respects_already_merged(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday EOD")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    # Reconciling id2 again should be a no-op since it's already merged.
    outcome = reconcile.reconcile_one(conn, id2)
    assert outcome is None


def test_reconcile_threshold_blocks_weak_matches(conn: sqlite3.Connection) -> None:
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 by Friday")
    _seed_commitment(conn, actor="Qat", deliverable="ship v0 next week sometime")
    # at threshold=100, no merge
    result = reconcile.reconcile_all(conn, threshold=100)
    assert result.linked == 0


# ----- folded queries -----


def test_canonical_view_folds_sources(conn: sqlite3.Connection) -> None:
    """After merging, the canonical entity surfaces sources from both originals."""
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0", quote="quote A")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by EOD", quote="quote B")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    items = state.list_commitments(conn)
    assert len(items) == 1
    folded = items[0]
    quotes = [s["verbatim_quote"] for s in folded["sources"]]
    assert "quote A" in quotes
    assert "quote B" in quotes
    assert folded["merged_count"] == 1


def test_ungrouped_view_shows_both(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by EOD")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    items = state.list_commitments(conn, canonical_only=False)
    assert len(items) == 2


# ----- auto-reconcile during ingest -----


def test_save_extraction_with_auto_reconcile_links(conn: sqlite3.Connection) -> None:
    # First extraction
    r1 = ExtractionResult(
        meeting_summary="m1", participants=["Qat"],
        commitments=[Commitment(
            actor="Qat", deliverable="ship v0 by Friday",
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="x", speaker="Qat", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model="m", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, r1, diag, source_path="meeting1")

    # Second extraction with a similar commitment, auto-reconcile on
    r2 = ExtractionResult(
        meeting_summary="m2", participants=["Qat"],
        commitments=[Commitment(
            actor="Qat", deliverable="ship v0 by friday EOD",
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="y", speaker="Qat", rationale="r")],
        )],
    )
    summary = state.save_extraction(
        conn, r2, diag, source_path="meeting2", auto_reconcile=True,
    )
    assert summary.reconcile_links == 1
    # Canonical view should have 1 commitment with 2 sources
    items = state.list_commitments(conn)
    assert len(items) == 1
    assert len(items[0]["sources"]) == 2


# ----- show_entity -----


def test_show_entity_includes_merged_sources(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="ship v0", quote="quote A")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="ship v0 by EOD", quote="quote B")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    detail = state.show_entity(conn, id1)
    assert detail is not None
    quotes = [s["verbatim_quote"] for s in detail["sources"]]
    assert "quote A" in quotes
    assert "quote B" in quotes


# ----- stats -----


def test_db_stats_counts_only_canonicals(conn: sqlite3.Connection) -> None:
    id1 = _seed_commitment(conn, actor="Qat", deliverable="x")
    id2 = _seed_commitment(conn, actor="Qat", deliverable="x duplicate")
    reconcile.link_entities(conn, canonical_id=id1, member_id=id2)
    s = state.stats(conn)
    assert s["commitments_open"] == 1
    assert s["entities_merged"] == 1
