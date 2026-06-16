"""Tests for the calendar connector — Google Calendar + Outlook (Graph) event
ingestion, the CalendarEvent unit shape, date parsing, and event filtering."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from verbatim.connectors import calendar as cal

# ----- fake HTTP transports -----


def _make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ----- CalendarEvent unit shape -----


def test_event_transcript_includes_agenda_and_attendees() -> None:
    event = cal.CalendarEvent(
        source="google",
        event_id="abc",
        title="Verbatim launch sync",
        description="Decide the launch date. Alice to prep the HN post.",
        organizer="alice@example.com",
        start=datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc),
        attendees=["Alice", "Bob"],
        location="Zoom",
    )
    t = event.transcript
    assert "Verbatim launch sync" in t
    assert "Alice, Bob" in t
    assert "Decide the launch date" in t
    assert "Zoom" in t
    assert "2026-05-20 14:00 UTC" in t


def test_event_source_kind_and_label() -> None:
    event = cal.CalendarEvent(
        source="outlook", event_id="evt-99", title="x", description="y",
        organizer="o", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert event.source_kind == "calendar_outlook"
    assert event.source_label == "calendar://outlook/evt-99"


def test_event_transcript_handles_missing_description() -> None:
    event = cal.CalendarEvent(
        source="google", event_id="e", title="Quick chat", description="",
        organizer="o", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert "(no description provided)" in event.transcript


def test_has_content_true_when_description_present() -> None:
    event = cal.CalendarEvent(
        source="google", event_id="e", title="t", description="real agenda",
        organizer="o", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert event.has_content is True


def test_has_content_true_when_multiple_attendees() -> None:
    event = cal.CalendarEvent(
        source="google", event_id="e", title="t", description="",
        organizer="o", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attendees=["A", "B"],
    )
    assert event.has_content is True


def test_has_content_false_for_empty_placeholder() -> None:
    event = cal.CalendarEvent(
        source="google", event_id="e", title="Focus time", description="",
        organizer="o", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attendees=["just me"],
    )
    assert event.has_content is False


# ----- Google Calendar client -----


def test_google_client_requires_token() -> None:
    with pytest.raises(ValueError, match="token is required"):
        cal.GoogleCalendarClient(token="")


def test_google_iter_events_parses_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "calendars/primary/events" in str(request.url)
        return httpx.Response(200, json={
            "items": [
                {
                    "id": "g1",
                    "status": "confirmed",
                    "summary": "Roadmap review",
                    "description": "Agenda: ship v0.11",
                    "organizer": {"email": "alice@example.com"},
                    "attendees": [
                        {"displayName": "Alice"},
                        {"email": "bob@example.com"},
                    ],
                    "start": {"dateTime": "2026-05-20T14:00:00Z"},
                    "end": {"dateTime": "2026-05-20T15:00:00Z"},
                    "location": "Meet",
                    "htmlLink": "https://cal/g1",
                },
            ],
        })

    with cal.GoogleCalendarClient(token="t", client=_make_client(handler)) as gc:
        events = list(gc.iter_events())
    assert len(events) == 1
    e = events[0]
    assert e.title == "Roadmap review"
    assert e.organizer == "alice@example.com"
    assert e.attendees == ["Alice", "bob@example.com"]
    assert e.source == "google"


def test_google_iter_events_skips_cancelled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [
                {"id": "g1", "status": "cancelled",
                 "start": {"dateTime": "2026-05-20T14:00:00Z"}},
                {"id": "g2", "status": "confirmed", "summary": "Live one",
                 "start": {"dateTime": "2026-05-21T14:00:00Z"}},
            ],
        })

    with cal.GoogleCalendarClient(token="t", client=_make_client(handler)) as gc:
        events = list(gc.iter_events())
    assert [e.event_id for e in events] == ["g2"]


def test_google_iter_events_follows_page_token() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken")
        calls.append(token or "")
        if not token:
            return httpx.Response(200, json={
                "items": [{"id": "g1", "status": "confirmed", "summary": "p1",
                           "start": {"dateTime": "2026-05-20T14:00:00Z"}}],
                "nextPageToken": "PAGE2",
            })
        return httpx.Response(200, json={
            "items": [{"id": "g2", "status": "confirmed", "summary": "p2",
                       "start": {"dateTime": "2026-05-21T14:00:00Z"}}],
        })

    with cal.GoogleCalendarClient(token="t", client=_make_client(handler)) as gc:
        events = list(gc.iter_events())
    assert [e.event_id for e in events] == ["g1", "g2"]
    assert "PAGE2" in calls


def test_google_iter_events_respects_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [
                {"id": f"g{i}", "status": "confirmed", "summary": f"e{i}",
                 "start": {"dateTime": "2026-05-20T14:00:00Z"}}
                for i in range(10)
            ],
        })

    with cal.GoogleCalendarClient(token="t", client=_make_client(handler)) as gc:
        events = list(gc.iter_events(limit=3))
    assert len(events) == 3


def test_google_all_day_event_parses_date_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{"id": "g1", "status": "confirmed", "summary": "Offsite",
                       "start": {"date": "2026-06-01"}}],
        })

    with cal.GoogleCalendarClient(token="t", client=_make_client(handler)) as gc:
        events = list(gc.iter_events())
    assert events[0].start.year == 2026
    assert events[0].start.month == 6


# ----- Outlook / Graph client -----


def test_outlook_client_requires_token() -> None:
    with pytest.raises(ValueError, match="token is required"):
        cal.OutlookCalendarClient(token="")


def test_outlook_iter_events_parses_value_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "me/calendarView" in str(request.url)
        return httpx.Response(200, json={
            "value": [
                {
                    "id": "o1",
                    "isCancelled": False,
                    "subject": "Sprint planning",
                    "bodyPreview": "Plan the sprint",
                    "organizer": {"emailAddress": {"name": "Bob"}},
                    "attendees": [
                        {"emailAddress": {"name": "Alice"}},
                        {"emailAddress": {"address": "carol@example.com"}},
                    ],
                    "start": {"dateTime": "2026-05-20T09:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-05-20T10:00:00", "timeZone": "UTC"},
                    "location": {"displayName": "Teams"},
                    "webLink": "https://outlook/o1",
                },
            ],
        })

    with cal.OutlookCalendarClient(token="t", client=_make_client(handler)) as oc:
        events = list(oc.iter_events())
    assert len(events) == 1
    e = events[0]
    assert e.title == "Sprint planning"
    assert e.organizer == "Bob"
    assert e.attendees == ["Alice", "carol@example.com"]
    assert e.location == "Teams"
    assert e.source == "outlook"


def test_outlook_iter_events_skips_cancelled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "value": [
                {"id": "o1", "isCancelled": True,
                 "start": {"dateTime": "2026-05-20T09:00:00"}},
                {"id": "o2", "isCancelled": False, "subject": "Live",
                 "start": {"dateTime": "2026-05-21T09:00:00"}},
            ],
        })

    with cal.OutlookCalendarClient(token="t", client=_make_client(handler)) as oc:
        events = list(oc.iter_events())
    assert [e.event_id for e in events] == ["o2"]


def test_outlook_iter_events_follows_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "skiptoken" not in str(request.url):
            return httpx.Response(200, json={
                "value": [{"id": "o1", "isCancelled": False, "subject": "p1",
                           "start": {"dateTime": "2026-05-20T09:00:00"}}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?skiptoken=X",
            })
        return httpx.Response(200, json={
            "value": [{"id": "o2", "isCancelled": False, "subject": "p2",
                       "start": {"dateTime": "2026-05-21T09:00:00"}}],
        })

    with cal.OutlookCalendarClient(token="t", client=_make_client(handler)) as oc:
        events = list(oc.iter_events())
    assert [e.event_id for e in events] == ["o1", "o2"]


def test_outlook_prefers_plain_text_body_over_preview() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "value": [{
                "id": "o1", "isCancelled": False, "subject": "x",
                "bodyPreview": "short preview",
                "body": {"contentType": "text", "content": "full plain body"},
                "start": {"dateTime": "2026-05-20T09:00:00"},
            }],
        })

    with cal.OutlookCalendarClient(token="t", client=_make_client(handler)) as oc:
        events = list(oc.iter_events())
    assert events[0].description == "full plain body"


def test_outlook_html_body_falls_back_to_preview() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "value": [{
                "id": "o1", "isCancelled": False, "subject": "x",
                "bodyPreview": "the preview text",
                "body": {"contentType": "html", "content": "<p>html junk</p>"},
                "start": {"dateTime": "2026-05-20T09:00:00"},
            }],
        })

    with cal.OutlookCalendarClient(token="t", client=_make_client(handler)) as oc:
        events = list(oc.iter_events())
    assert events[0].description == "the preview text"


# ----- shared helpers -----


def test_parse_iso_handles_z_suffix() -> None:
    dt = cal._parse_iso("2026-05-20T14:00:00Z")
    assert dt.tzinfo is not None
    assert dt.hour == 14


def test_parse_iso_handles_date_only() -> None:
    dt = cal._parse_iso("2026-06-01")
    assert dt.year == 2026 and dt.month == 6 and dt.day == 1


def test_rfc3339_renders_utc_z() -> None:
    dt = datetime(2026, 5, 20, 14, 30, 0, tzinfo=timezone.utc)
    assert cal._rfc3339(dt) == "2026-05-20T14:30:00Z"


def test_rfc3339_assumes_utc_for_naive() -> None:
    dt = datetime(2026, 5, 20, 14, 30, 0)
    assert cal._rfc3339(dt).endswith("Z")
