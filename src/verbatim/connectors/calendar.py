"""Calendar connectors — pull meeting events from Google Calendar and
Microsoft Outlook (Graph) as extraction units.

Most teams put the meeting agenda, the pre-read, and often the decisions and
action items directly into the calendar event description. That text is a
genuine extraction source — Verbatim treats each event as one unit, the same
shape as a meeting transcript or a PR thread.

# What this connector does (and doesn't)

It ingests the *event*: title, organizer, attendees, location, and the full
description / body. It does **not** fetch the meeting *recording transcript* —
that lives in Drive (Google Meet) or SharePoint/Stream (Teams) behind separate
APIs and OAuth scopes. Pairing an event with its recording transcript is a
later milestone; for now the event body is the payload.

# Auth

Both APIs take an OAuth 2.0 bearer access token. This connector does not run
the OAuth dance itself — pass an already-obtained access token:

- Google: a token with the `calendar.readonly` scope. Obtain via the OAuth
  playground (developers.google.com/oauthplayground) or your own flow. Pass
  via `$GOOGLE_CALENDAR_TOKEN` or `--token`.
- Outlook / Microsoft 365: a token with the `Calendars.Read` Graph scope.
  Obtain via the Graph Explorer or an Azure AD app. Pass via
  `$OUTLOOK_CALENDAR_TOKEN` or `--token`.

Access tokens are short-lived (≈1 hour). For unattended/daemon use, wire a
refresh-token flow upstream and feed this connector a fresh token per run.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
MS_GRAPH_API = "https://graph.microsoft.com/v1.0"


@dataclass
class CalendarEvent:
    """One calendar event, ready to extract."""

    source: str  # "google" | "outlook"
    event_id: str
    title: str
    description: str
    organizer: str
    start: datetime
    end: datetime | None = None
    attendees: list[str] = field(default_factory=list)
    location: str | None = None
    html_link: str | None = None

    @property
    def transcript(self) -> str:
        when = self.start.strftime("%Y-%m-%d %H:%M UTC")
        lines: list[str] = [
            f"Calendar event: {self.title}",
            f"When: {when}",
            f"Organizer: {self.organizer}",
        ]
        if self.attendees:
            lines.append(f"Attendees: {', '.join(self.attendees)}")
        if self.location:
            lines.append(f"Location: {self.location}")
        lines.append("")
        lines.append("# Agenda / description")
        body = (self.description or "").strip()
        lines.append(body if body else "(no description provided)")
        return "\n".join(lines).rstrip() + "\n"

    @property
    def source_kind(self) -> str:
        return f"calendar_{self.source}"

    @property
    def source_label(self) -> str:
        return f"calendar://{self.source}/{self.event_id}"

    @property
    def has_content(self) -> bool:
        """True if the event carries enough text to be worth extracting.

        An event with no description and no attendees is just a placeholder
        on someone's calendar — extracting it spends tokens for nothing.
        """
        return bool((self.description or "").strip()) or len(self.attendees) > 1


# ----------------------- Google Calendar -----------------------


class GoogleCalendarClient:
    """Minimal Google Calendar API v3 client for event ingestion."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GOOGLE_CALENDAR_API,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError(
                "Google Calendar token is required "
                "(set GOOGLE_CALENDAR_TOKEN or pass --token)."
            )
        self._base = base_url.rstrip("/")
        self._owned_client = client is None
        self._http = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "verbatim/0.x",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owned_client:
            self._http.close()

    def __enter__(self) -> GoogleCalendarClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def iter_events(
        self,
        *,
        calendar_id: str = "primary",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[CalendarEvent]:
        """Yield CalendarEvents from one Google calendar in a date window.

        `singleEvents=true` expands recurring events into individual
        instances so each occurrence is its own unit.
        """
        params: dict[str, str | int] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if since is not None:
            params["timeMin"] = _rfc3339(since)
        if until is not None:
            params["timeMax"] = _rfc3339(until)

        yielded = 0
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = self._http.get(
                f"{self._base}/calendars/{calendar_id}/events", params=params
            )
            resp.raise_for_status()
            data = resp.json()
            for raw in data.get("items", []):
                event = _google_event(raw)
                if event is None:
                    continue
                yield event
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            page_token = data.get("nextPageToken")
            if not page_token:
                return


def _google_event(raw: dict) -> CalendarEvent | None:
    """Map one Google Calendar event JSON object to a CalendarEvent."""
    if raw.get("status") == "cancelled":
        return None
    start = _google_dt(raw.get("start"))
    if start is None:
        return None
    organizer = (raw.get("organizer") or {}).get("email") or (
        raw.get("organizer") or {}
    ).get("displayName") or "unknown"
    attendees = [
        a.get("displayName") or a.get("email") or "unknown"
        for a in raw.get("attendees", [])
    ]
    return CalendarEvent(
        source="google",
        event_id=raw.get("id") or "",
        title=raw.get("summary") or "(untitled event)",
        description=raw.get("description") or "",
        organizer=organizer,
        start=start,
        end=_google_dt(raw.get("end")),
        attendees=attendees,
        location=raw.get("location"),
        html_link=raw.get("htmlLink"),
    )


def _google_dt(slot: dict | None) -> datetime | None:
    """Parse a Google Calendar start/end slot ({dateTime} or all-day {date})."""
    if not slot:
        return None
    raw = slot.get("dateTime") or slot.get("date")
    if not raw:
        return None
    return _parse_iso(raw)


# ----------------------- Microsoft Outlook (Graph) -----------------------


class OutlookCalendarClient:
    """Minimal Microsoft Graph client for Outlook calendar ingestion."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = MS_GRAPH_API,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError(
                "Outlook calendar token is required "
                "(set OUTLOOK_CALENDAR_TOKEN or pass --token)."
            )
        self._base = base_url.rstrip("/")
        self._owned_client = client is None
        self._http = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "verbatim/0.x",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owned_client:
            self._http.close()

    def __enter__(self) -> OutlookCalendarClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def iter_events(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[CalendarEvent]:
        """Yield CalendarEvents from the signed-in user's Outlook calendar.

        Uses Graph's `calendarView`, which expands recurring events into
        instances over the requested window. When `since`/`until` are not
        given, defaults to a wide window so the call is still valid.
        """
        start = since or datetime(2000, 1, 1, tzinfo=timezone.utc)
        end = until or datetime(2100, 1, 1, tzinfo=timezone.utc)
        url: str | None = f"{self._base}/me/calendarView"
        params: dict[str, str | int] | None = {
            "startDateTime": _rfc3339(start),
            "endDateTime": _rfc3339(end),
            "$orderby": "start/dateTime",
            "$top": 100,
        }

        yielded = 0
        while url:
            resp = self._http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for raw in data.get("value", []):
                event = _outlook_event(raw)
                if event is None:
                    continue
                yield event
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            # Graph paginates via an absolute @odata.nextLink (params baked in).
            url = data.get("@odata.nextLink")
            params = None


def _outlook_event(raw: dict) -> CalendarEvent | None:
    """Map one Microsoft Graph event JSON object to a CalendarEvent."""
    if raw.get("isCancelled"):
        return None
    start = _outlook_dt(raw.get("start"))
    if start is None:
        return None
    organizer = (
        ((raw.get("organizer") or {}).get("emailAddress") or {}).get("name")
        or ((raw.get("organizer") or {}).get("emailAddress") or {}).get("address")
        or "unknown"
    )
    attendees = [
        (a.get("emailAddress") or {}).get("name")
        or (a.get("emailAddress") or {}).get("address")
        or "unknown"
        for a in raw.get("attendees", [])
    ]
    body = raw.get("body") or {}
    # Graph bodies are HTML by default; bodyPreview is plain text.
    description = raw.get("bodyPreview") or ""
    if body.get("contentType") == "text" and body.get("content"):
        description = body["content"]
    location = (raw.get("location") or {}).get("displayName")
    return CalendarEvent(
        source="outlook",
        event_id=raw.get("id") or "",
        title=raw.get("subject") or "(untitled event)",
        description=description,
        organizer=organizer,
        start=start,
        end=_outlook_dt(raw.get("end")),
        attendees=attendees,
        location=location,
        html_link=raw.get("webLink"),
    )


def _outlook_dt(slot: dict | None) -> datetime | None:
    """Parse a Graph start/end slot ({dateTime, timeZone})."""
    if not slot:
        return None
    raw = slot.get("dateTime")
    if not raw:
        return None
    dt = _parse_iso(raw)
    # Graph returns naive dateTimes paired with a timeZone field; the default
    # response timeZone is UTC unless the caller sets a Prefer header.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ----------------------- shared helpers -----------------------


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and date-only forms."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # date-only (all-day event): treat as midnight UTC
        dt = datetime.fromisoformat(text + "T00:00:00+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _rfc3339(dt: datetime) -> str:
    """Render a datetime as an RFC-3339 string both APIs accept."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
