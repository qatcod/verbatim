"""GitHub Issues projection — push Verbatim commitments out as GitHub issues.

Mirrors the Linear projection's interface (LinearClient → GitHubIssuesClient,
TARGET_KIND, plan_projection / execute_projection / deactivate_projection) so
the orchestration code in the CLI is the same shape for every projection target.

# Auth
GitHub Personal Access Token (PAT). Classic PAT needs `repo` scope; fine-grained
needs Read+Write on Issues for the target repo. Read via `$GITHUB_TOKEN`.

# Mapping

| Verbatim field           | GitHub field          |
|--------------------------|------------------------|
| commitment.deliverable   | issue.title (prefixed) |
| commitment.actor         | issue.assignees[0]     |
| sources[*].verbatim_quote| issue.body             |
| entity.id                | embedded in issue.body |
| (CLI flag)               | issue.labels           |

Assignee resolution uses the actor string directly as a GitHub login (best-
effort). If the user doesn't exist as a collaborator, GitHub silently drops the
assignee — we don't pre-check.

# Idempotency
Tracked through the shared `projections` table with `target_kind='github_issue'`.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from .. import store
from .linear import _empty_draft  # type: ignore[attr-defined]

TARGET_KIND = "github_issue"


class GitHubIssuesClient:
    """Thin httpx wrapper over the GitHub REST API for issue ops."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError("GitHub token is required (set GITHUB_TOKEN or pass --token).")
        self._token = token
        self._base = base_url.rstrip("/")
        self._owned = client is None
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
        if self._owned:
            self._http.close()

    def __enter__(self) -> GitHubIssuesClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def viewer_login(self) -> str | None:
        resp = self._http.get(f"{self._base}/user")
        resp.raise_for_status()
        return resp.json().get("login")

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title[:256], "body": body}
        if assignees:
            payload["assignees"] = assignees
        if labels:
            payload["labels"] = labels
        resp = self._http.post(f"{self._base}/repos/{repo}/issues", json=payload)
        resp.raise_for_status()
        return resp.json()

    def close_issue(self, *, repo: str, number: int) -> bool:
        resp = self._http.patch(
            f"{self._base}/repos/{repo}/issues/{number}",
            json={"state": "closed"},
        )
        resp.raise_for_status()
        return resp.json().get("state") == "closed"


@dataclass
class IssueDraft:
    title: str
    body: str
    assignees: list[str]
    labels: list[str]


def render_issue_from_commitment(
    entity: dict[str, Any],
    *,
    extra_labels: list[str] | None = None,
) -> IssueDraft:
    payload = entity.get("payload", {})
    actor = payload.get("actor")
    deliverable = payload.get("deliverable") or "(no deliverable)"
    title = f"[verbatim] {actor}: {deliverable}" if actor else f"[verbatim] {deliverable}"

    lines: list[str] = []
    lines.append(f"**Verbatim-extracted commitment** ({entity.get('confidence', 'unknown')} confidence)")
    if payload.get("deadline"):
        lines.append(f"\n- **Deadline:** {payload['deadline']}")
    if payload.get("to"):
        lines.append(f"- **To:** {payload['to']}")
    if payload.get("notes"):
        lines.append(f"- **Notes:** {payload['notes']}")
    lines.append("\n## Supporting quotes\n")
    for s in entity.get("sources", []):
        speaker = s.get("speaker") or "unknown"
        ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
        lines.append(f"> {ts}**{speaker}:** {s['verbatim_quote']}")
        if s.get("rationale"):
            lines.append(f"  _extractor rationale: {s['rationale']}_")
        lines.append("")
    lines.append(f"\n_Verbatim entity id: `{entity['id']}`_")

    return IssueDraft(
        title=title,
        body="\n".join(lines).rstrip(),
        assignees=[actor] if actor else [],
        labels=["verbatim", *(extra_labels or [])],
    )


@dataclass
class ProjectionPlan:
    entity: dict[str, Any]
    draft: IssueDraft
    skip_reason: str | None = None


def plan_projection(
    conn: sqlite3.Connection,
    entity: dict[str, Any],
    *,
    extra_labels: list[str] | None = None,
) -> ProjectionPlan:
    if entity.get("canonical_id") is not None:
        return ProjectionPlan(
            entity=entity,
            draft=IssueDraft("", "", [], []),
            skip_reason="non-canonical (merged sibling)",
        )
    existing = store.find_active_projection(
        conn, entity_id=entity["id"], target_kind=TARGET_KIND
    )
    if existing is not None:
        ident = (existing.get("metadata") or {}).get("number") or existing.get("external_id")
        return ProjectionPlan(
            entity=entity,
            draft=IssueDraft("", "", [], []),
            skip_reason=f"already projected as #{ident}",
        )
    return ProjectionPlan(
        entity=entity,
        draft=render_issue_from_commitment(entity, extra_labels=extra_labels),
    )


def execute_projection(
    conn: sqlite3.Connection,
    client: GitHubIssuesClient,
    plan: ProjectionPlan,
    *,
    repo: str,
) -> dict[str, Any]:
    if plan.skip_reason:
        raise RuntimeError(f"Plan marked skip: {plan.skip_reason}")
    d = plan.draft
    issue = client.create_issue(
        repo=repo, title=d.title, body=d.body,
        assignees=d.assignees, labels=d.labels,
    )
    projection_id = store.insert_projection(
        conn,
        entity_id=plan.entity["id"],
        target_kind=TARGET_KIND,
        external_id=str(issue.get("id")),
        external_url=issue.get("html_url"),
        metadata={
            "number": issue.get("number"),
            "title": issue.get("title"),
            "repo": repo,
            "labels": d.labels,
            "assignees": d.assignees,
        },
    )
    return {
        "projection_id": projection_id,
        "external_id": str(issue.get("id")),
        "external_url": issue.get("html_url"),
        "number": issue.get("number"),
    }


def deactivate_projection(
    conn: sqlite3.Connection,
    projection_id: str,
    *,
    client: GitHubIssuesClient | None = None,
    close_external: bool = False,
) -> bool:
    if close_external and client is not None:
        row = conn.execute(
            "SELECT external_id, metadata_json FROM projections WHERE id = ?",
            (projection_id,),
        ).fetchone()
        if row:
            import json
            meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            repo = meta.get("repo")
            number = meta.get("number")
            if repo and number:
                try:
                    client.close_issue(repo=repo, number=number)
                except Exception:  # noqa: BLE001
                    pass
    return store.update_projection_status(conn, projection_id, "inactive")


# Reference to the linear helper just to keep API surface consistent — unused
# externally but documents that the import is intentional.
_ = _empty_draft
