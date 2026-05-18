"""GitHub PR connector tests — all responses mocked via httpx MockTransport."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from verbatim.connectors import github_pr


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> github_pr.GitHubClient:
    """Build a GitHubClient whose underlying httpx hits the given handler."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        headers={
            "Authorization": "Bearer test-token",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "verbatim/test",
        },
    )
    return github_pr.GitHubClient(token="test-token", client=http)


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())


# ----- constructor -----


def test_constructor_requires_token() -> None:
    with pytest.raises(ValueError):
        github_pr.GitHubClient(token="")


# ----- fetch_pull_request -----


def test_fetch_pull_request_assembles_unit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/qatcod/verbatim/pulls/42":
            return json_response({
                "number": 42,
                "title": "Add Slack connector",
                "user": {"login": "qatcod"},
                "body": "This adds the Slack export parser.",
                "created_at": "2026-05-18T10:00:00Z",
                "state": "closed",
                "merged_at": "2026-05-18T12:00:00Z",
                "html_url": "https://github.com/qatcod/verbatim/pull/42",
            })
        if request.url.path == "/repos/qatcod/verbatim/issues/42/comments":
            return json_response([
                {
                    "user": {"login": "jasonr"},
                    "body": "Looking good. One nit on the rendering format.",
                    "created_at": "2026-05-18T10:30:00Z",
                },
            ])
        if request.url.path == "/repos/qatcod/verbatim/pulls/42/comments":
            return json_response([
                {
                    "user": {"login": "tazj"},
                    "body": "Should this be a wildcard match?",
                    "path": "src/verbatim/connectors/slack_export.py",
                    "line": 87,
                    "created_at": "2026-05-18T10:15:00Z",
                },
            ])
        return httpx.Response(404)

    client = make_client(handler)
    unit = client.fetch_pull_request("qatcod/verbatim", 42)
    client.close()

    assert unit.number == 42
    assert unit.title == "Add Slack connector"
    assert unit.author == "qatcod"
    assert unit.state == "merged"
    assert len(unit.comments) == 2
    issue_authors = {c.author for c in unit.comments if c.kind == "issue"}
    review_authors = {c.author for c in unit.comments if c.kind == "review"}
    assert issue_authors == {"jasonr"}
    assert review_authors == {"tazj"}


def test_transcript_sorts_chronologically_and_includes_review_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/42" in request.url.path and "/comments" not in request.url.path:
            return json_response({
                "number": 42, "title": "Test", "user": {"login": "q"},
                "body": "desc.", "created_at": "2026-05-18T10:00:00Z",
                "state": "open", "html_url": "x",
            })
        if "/issues/42/comments" in request.url.path:
            return json_response([
                {"user": {"login": "later"}, "body": "second",
                 "created_at": "2026-05-18T12:00:00Z"},
            ])
        if "/pulls/42/comments" in request.url.path:
            return json_response([
                {"user": {"login": "earlier"}, "body": "first",
                 "path": "foo.py", "line": 5,
                 "created_at": "2026-05-18T11:00:00Z"},
            ])
        return httpx.Response(404)

    client = make_client(handler)
    unit = client.fetch_pull_request("o/r", 42)
    client.close()

    text = unit.transcript
    earlier_idx = text.index("first")
    later_idx = text.index("second")
    assert earlier_idx < later_idx
    assert "@earlier (review on foo.py:5)" in text
    assert "@later" in text


def test_source_label_and_kind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/7" in request.url.path and "/comments" not in request.url.path:
            return json_response({
                "number": 7, "title": "t", "user": {"login": "u"},
                "body": "", "created_at": "2026-05-18T00:00:00Z",
                "state": "open", "html_url": "",
            })
        return json_response([])

    client = make_client(handler)
    unit = client.fetch_pull_request("acme/widget", 7)
    client.close()
    assert unit.source_label == "github://acme/widget/pull/7"
    assert unit.source_kind == "github_pr"


# ----- list_pull_request_numbers -----


def test_list_pull_request_numbers_paginates_and_filters_by_since() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        page = request.url.params.get("page", "1")
        if page == "1":
            return json_response([
                {"number": 100, "updated_at": "2026-05-18T10:00:00Z"},
                {"number": 99, "updated_at": "2026-05-17T10:00:00Z"},
            ])
        if page == "2":
            return json_response([
                {"number": 98, "updated_at": "2026-04-30T10:00:00Z"},
            ])
        return json_response([])

    client = make_client(handler)
    from datetime import datetime, timezone
    nums = client.list_pull_request_numbers(
        "o/r",
        state="all",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    client.close()
    # 100 and 99 are after 2026-05-01; 98 is before → stops scan
    assert nums == [100, 99]


# ----- iter_pull_requests -----


def test_iter_pull_requests_with_explicit_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1") or request.url.path.endswith("/pulls/2"):
            num = int(request.url.path.rsplit("/", 1)[-1])
            return json_response({
                "number": num, "title": f"PR {num}", "user": {"login": "u"},
                "body": "", "created_at": "2026-05-18T00:00:00Z",
                "state": "open", "html_url": "",
            })
        return json_response([])

    client = make_client(handler)
    units = list(client.iter_pull_requests("o/r", numbers=[1, 2]))
    client.close()
    assert [u.number for u in units] == [1, 2]


# ----- error handling -----


def test_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad creds"})

    client = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_pull_request("o/r", 1)
    client.close()


# ----- helper tests -----


def test_parse_iso_handles_z_and_offset_format() -> None:
    from datetime import timezone
    d1 = github_pr._parse_iso("2024-01-15T10:30:00Z")
    d2 = github_pr._parse_iso("2024-01-15T10:30:00+00:00")
    assert d1 == d2
    assert d1.tzinfo == timezone.utc


def test_parse_iso_handles_none_returns_now() -> None:
    d = github_pr._parse_iso(None)
    assert d.tzinfo is not None  # tz-aware
