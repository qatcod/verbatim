"""GitHub Pull Request connector — pull PR discussion threads as extraction units.

Most engineering decisions and commitments actually happen in PR review threads,
not in scheduled meetings. This connector pulls a PR's body, all issue comments,
and all review comments, sorts them chronologically, and produces one extraction
unit per PR — the same shape the rest of the pipeline consumes.

# Auth

Provide a GitHub Personal Access Token (PAT) via `$GITHUB_TOKEN` or `--token`.
A classic PAT needs `repo` scope (public_repo for public-only). A fine-grained
PAT needs Read access to "Pull requests" and "Issues" on the repos you want
to ingest from.

# Rate limits

Authenticated requests get 5,000/hour per account — plenty for batch ingest.
Errors bubble up; we don't auto-retry.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"


@dataclass
class GitHubComment:
    """One comment on a PR — either an issue comment or a review (line) comment."""

    author: str
    body: str
    created_at: datetime
    kind: str  # "issue" or "review"
    path: str | None = None  # for review comments: the file the line is on
    line: int | None = None  # for review comments: the line number

    @property
    def header(self) -> str:
        ts = self.created_at.strftime("%Y-%m-%d %H:%M UTC")
        if self.kind == "review" and self.path:
            loc = f" on {self.path}"
            if self.line is not None:
                loc += f":{self.line}"
            return f"[{ts}] @{self.author} (review{loc})"
        return f"[{ts}] @{self.author}"


@dataclass
class PullRequestUnit:
    """One PR's full discussion, ready to extract."""

    repo: str  # "owner/name"
    number: int
    title: str
    author: str
    body: str
    created_at: datetime
    state: str  # "open", "closed", "merged"
    comments: list[GitHubComment]
    html_url: str

    @property
    def transcript(self) -> str:
        lines: list[str] = [
            f"Repository: {self.repo}",
            f"PR #{self.number}: {self.title}",
            f"Opened by @{self.author} on {self.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"State: {self.state}",
            "",
            "# PR description",
            self.body.strip() if self.body and self.body.strip() else "(no description)",
            "",
            "# Discussion",
        ]
        for c in sorted(self.comments, key=lambda x: x.created_at):
            lines.append(c.header)
            lines.append(c.body.strip())
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @property
    def source_kind(self) -> str:
        return "github_pr"

    @property
    def source_label(self) -> str:
        return f"github://{self.repo}/pull/{self.number}"


class GitHubClient:
    """Minimal GitHub REST client for PR ingestion.

    Only depends on httpx — no PyGithub or other heavy SDK. Adds Bearer auth
    and the GitHub API version header. Pagination is handled per-endpoint.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GITHUB_API,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError("GitHub token is required (set GITHUB_TOKEN or pass --token).")
        self._token = token
        self._base = base_url.rstrip("/")
        self._owned_client = client is None
        self._http = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "verbatim/0.x",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owned_client:
            self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- PR listing -----

    def list_pull_request_numbers(
        self,
        repo: str,
        *,
        state: str = "all",
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[int]:
        """List PR numbers for a repo, optionally filtered by created/updated time.

        GitHub's pulls API uses `state=open|closed|all`. We filter by `updated_at`
        client-side because the API only supports sort on updated, not a since
        filter directly.
        """
        numbers: list[int] = []
        page = 1
        while True:
            resp = self._get(
                f"/repos/{repo}/pulls",
                params={
                    "state": state,
                    "per_page": 100,
                    "page": page,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
            page_data = resp.json()
            if not page_data:
                break
            stop = False
            for pr in page_data:
                updated = _parse_iso(pr.get("updated_at"))
                if since and updated < since:
                    stop = True
                    break
                if until and updated > until:
                    continue
                numbers.append(int(pr["number"]))
            if stop or len(page_data) < 100:
                break
            page += 1
        return numbers

    # ----- PR detail -----

    def fetch_pull_request(self, repo: str, number: int) -> PullRequestUnit:
        """Fetch a single PR plus all its comments and return a PullRequestUnit."""
        pr = self._get(f"/repos/{repo}/pulls/{number}").json()
        issue_comments = self._fetch_issue_comments(repo, number)
        review_comments = self._fetch_review_comments(repo, number)
        state = pr.get("state", "unknown")
        if pr.get("merged_at"):
            state = "merged"
        return PullRequestUnit(
            repo=repo,
            number=number,
            title=pr.get("title") or f"PR #{number}",
            author=(pr.get("user") or {}).get("login") or "unknown",
            body=pr.get("body") or "",
            created_at=_parse_iso(pr.get("created_at")),
            state=state,
            comments=issue_comments + review_comments,
            html_url=pr.get("html_url") or "",
        )

    def iter_pull_requests(
        self,
        repo: str,
        *,
        state: str = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        numbers: list[int] | None = None,
    ) -> Iterator[PullRequestUnit]:
        """Yield PullRequestUnits for either explicit `numbers` or a state/date window."""
        if numbers:
            target_numbers = numbers
        else:
            target_numbers = self.list_pull_request_numbers(
                repo, state=state, since=since, until=until
            )
        for n in target_numbers:
            yield self.fetch_pull_request(repo, n)

    # ----- comment fetchers -----

    def _fetch_issue_comments(self, repo: str, number: int) -> list[GitHubComment]:
        out: list[GitHubComment] = []
        for raw in self._paginated(f"/repos/{repo}/issues/{number}/comments"):
            author = (raw.get("user") or {}).get("login") or "unknown"
            out.append(
                GitHubComment(
                    author=author,
                    body=raw.get("body") or "",
                    created_at=_parse_iso(raw.get("created_at")),
                    kind="issue",
                )
            )
        return out

    def _fetch_review_comments(self, repo: str, number: int) -> list[GitHubComment]:
        out: list[GitHubComment] = []
        for raw in self._paginated(f"/repos/{repo}/pulls/{number}/comments"):
            author = (raw.get("user") or {}).get("login") or "unknown"
            out.append(
                GitHubComment(
                    author=author,
                    body=raw.get("body") or "",
                    created_at=_parse_iso(raw.get("created_at")),
                    kind="review",
                    path=raw.get("path"),
                    line=raw.get("line") or raw.get("original_line"),
                )
            )
        return out

    # ----- HTTP plumbing -----

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = self._http.get(f"{self._base}{path}", params=params or {})
        resp.raise_for_status()
        return resp

    def _paginated(self, path: str, *, per_page: int = 100) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            resp = self._get(path, params={"per_page": per_page, "page": page})
            data = resp.json()
            if not data:
                return
            yield from data
            if len(data) < per_page:
                return
            page += 1


# ----- helpers -----


def _parse_iso(value: str | None) -> datetime:
    """Parse a GitHub ISO timestamp (always UTC, Z-suffix or +00:00)."""
    if not value:
        from datetime import timezone

        return datetime.now(tz=timezone.utc)
    # GitHub returns either '2024-01-15T10:30:00Z' or '2024-01-15T10:30:00+00:00'
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
