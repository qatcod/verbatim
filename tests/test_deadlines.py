"""Tests for the proactive deadline layer — verbatim.deadlines parsing +
classification, and state.{deadlined,overdue,due_soon}_commitments."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from starlette.testclient import TestClient

from verbatim import deadlines, state, web
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Commitment,
    Confidence,
    ExtractionResult,
    SourceReference,
)

# A fixed "today" so weekday/relative parsing is deterministic.
# 2026-05-20 is a Wednesday.
TODAY = date(2026, 5, 20)


# ----- parse_deadline -----


def test_parse_iso_date() -> None:
    assert deadlines.parse_deadline("2026-05-23", today=TODAY) == date(2026, 5, 23)


def test_parse_iso_datetime() -> None:
    assert deadlines.parse_deadline(
        "2026-05-23T10:00:00Z", today=TODAY
    ) == date(2026, 5, 23)


def test_parse_today_and_tomorrow() -> None:
    assert deadlines.parse_deadline("today", today=TODAY) == TODAY
    assert deadlines.parse_deadline("tomorrow", today=TODAY) == date(2026, 5, 21)


def test_parse_weekday_next_occurrence() -> None:
    # Wednesday → "Friday" is 2 days out
    assert deadlines.parse_deadline("Friday", today=TODAY) == date(2026, 5, 22)
    # "Monday" wraps to next week
    assert deadlines.parse_deadline("Monday", today=TODAY) == date(2026, 5, 25)


def test_parse_weekday_said_on_same_day_means_next_week() -> None:
    # On a Wednesday, "Wednesday" means next Wednesday — a deadline named for
    # today has effectively slipped.
    assert deadlines.parse_deadline("Wednesday", today=TODAY) == date(2026, 5, 27)


def test_parse_next_weekday_adds_a_week() -> None:
    assert deadlines.parse_deadline("next Friday", today=TODAY) == date(2026, 5, 29)


def test_parse_strips_eod_and_by_filler() -> None:
    assert deadlines.parse_deadline("EOD Friday", today=TODAY) == date(2026, 5, 22)
    assert deadlines.parse_deadline("by Friday", today=TODAY) == date(2026, 5, 22)
    assert deadlines.parse_deadline(
        "by end of day Friday", today=TODAY
    ) == date(2026, 5, 22)
    assert deadlines.parse_deadline("before Monday", today=TODAY) == date(2026, 5, 25)


def test_parse_in_n_days() -> None:
    assert deadlines.parse_deadline("in 3 days", today=TODAY) == date(2026, 5, 23)
    assert deadlines.parse_deadline("5 days", today=TODAY) == date(2026, 5, 25)


def test_parse_next_week() -> None:
    assert deadlines.parse_deadline("next week", today=TODAY) == date(2026, 5, 27)


def test_parse_month_day() -> None:
    assert deadlines.parse_deadline("May 23", today=TODAY) == date(2026, 5, 23)
    assert deadlines.parse_deadline("23 May", today=TODAY) == date(2026, 5, 23)
    assert deadlines.parse_deadline("May 23rd", today=TODAY) == date(2026, 5, 23)
    assert deadlines.parse_deadline("Jun 1 2027", today=TODAY) == date(2027, 6, 1)


def test_parse_month_day_past_rolls_to_next_year() -> None:
    # "January 5" said in May is most likely next January.
    assert deadlines.parse_deadline("January 5", today=TODAY) == date(2027, 1, 5)


def test_parse_unparseable_returns_none() -> None:
    assert deadlines.parse_deadline("sometime soon", today=TODAY) is None
    assert deadlines.parse_deadline("when it's ready", today=TODAY) is None
    assert deadlines.parse_deadline("", today=TODAY) is None
    assert deadlines.parse_deadline(None, today=TODAY) is None


# ----- due_status -----


def test_due_status_overdue() -> None:
    assert deadlines.due_status(date(2026, 5, 18), today=TODAY) == "overdue"


def test_due_status_today() -> None:
    assert deadlines.due_status(TODAY, today=TODAY) == "due_today"


def test_due_status_due_soon() -> None:
    assert deadlines.due_status(date(2026, 5, 25), today=TODAY) == "due_soon"


def test_due_status_scheduled_beyond_window() -> None:
    assert deadlines.due_status(date(2026, 6, 30), today=TODAY) == "scheduled"


def test_due_status_unknown_for_none() -> None:
    assert deadlines.due_status(None, today=TODAY) == "unknown"


def test_days_until_signed() -> None:
    assert deadlines.days_until(date(2026, 5, 25), today=TODAY) == 5
    assert deadlines.days_until(date(2026, 5, 18), today=TODAY) == -2
    assert deadlines.days_until(TODAY, today=TODAY) == 0


# ----- state-level helpers -----


def _seed(db_path: Path, commitments: list[tuple[str, str, str | None]]) -> None:
    """Seed commitments as (actor, deliverable, deadline) tuples."""
    conn = state.open_db(db_path)
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    try:
        state.save_extraction(
            conn,
            ExtractionResult(
                meeting_summary="seed", participants=["Qat"],
                commitments=[
                    Commitment(
                        actor=actor, deliverable=deliverable, deadline=deadline,
                        confidence=Confidence.HIGH,
                        sources=[SourceReference(
                            verbatim_quote=f"{actor}: {deliverable}",
                            speaker=actor, rationale="r",
                        )],
                    )
                    for actor, deliverable, deadline in commitments
                ],
            ),
            diag, source_path="m.txt",
        )
    finally:
        conn.close()


def test_deadlined_commitments_annotates_status(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed(db, [
        ("Qat", "overdue thing", "2026-05-15"),
        ("Jason", "soon thing", "2026-05-22"),
        ("Taz", "far thing", "2026-09-01"),
        ("Moe", "vague thing", "whenever"),
    ])
    conn = state.open_db(db)
    try:
        items = state.deadlined_commitments(conn, today=TODAY)
    finally:
        conn.close()
    by_deliverable = {i["payload"]["deliverable"]: i for i in items}
    assert by_deliverable["overdue thing"]["due_status"] == "overdue"
    assert by_deliverable["soon thing"]["due_status"] == "due_soon"
    assert by_deliverable["far thing"]["due_status"] == "scheduled"
    assert by_deliverable["vague thing"]["due_status"] == "unknown"


def test_deadlined_commitments_sorted_soonest_first(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed(db, [
        ("A", "far", "2026-09-01"),
        ("B", "overdue", "2026-05-10"),
        ("C", "soon", "2026-05-22"),
    ])
    conn = state.open_db(db)
    try:
        items = state.deadlined_commitments(conn, today=TODAY)
    finally:
        conn.close()
    order = [i["payload"]["deliverable"] for i in items]
    assert order == ["overdue", "soon", "far"]


def test_overdue_commitments_filters(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed(db, [
        ("A", "late", "2026-05-01"),
        ("B", "fine", "2026-08-01"),
    ])
    conn = state.open_db(db)
    try:
        overdue = state.overdue_commitments(conn, today=TODAY)
    finally:
        conn.close()
    assert len(overdue) == 1
    assert overdue[0]["payload"]["deliverable"] == "late"


def test_due_soon_commitments_filters(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed(db, [
        ("A", "this week", "2026-05-23"),
        ("B", "next month", "2026-06-30"),
        ("C", "already late", "2026-05-01"),
    ])
    conn = state.open_db(db)
    try:
        soon = state.due_soon_commitments(conn, today=TODAY)
    finally:
        conn.close()
    deliverables = {i["payload"]["deliverable"] for i in soon}
    assert deliverables == {"this week"}


# ----- /deadlines web route -----


def test_deadlines_route_renders(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed(db, [
        ("Qat", "overdue deliverable", "2026-05-01"),
        ("Jason", "soon deliverable", "2026-05-23"),
    ])
    client = TestClient(web.create_app(db_path=db))
    # The route uses the real today(); seed dates are relative to TODAY which
    # is in the past, so by real-time both are overdue. Either way the page
    # must render with the section structure.
    r = client.get("/deadlines")
    assert r.status_code == 200
    assert "Deadlines" in r.text


def test_deadlines_route_empty_state(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    state.open_db(db).close()
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/deadlines")
    assert r.status_code == 200
    assert "Nothing overdue or due soon" in r.text


def test_deadlines_link_in_sidebar(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    state.open_db(db).close()
    client = TestClient(web.create_app(db_path=db))
    r = client.get("/")
    assert 'href="/deadlines"' in r.text
