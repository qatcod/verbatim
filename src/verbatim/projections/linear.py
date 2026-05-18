"""Linear projection — push Verbatim commitments out as Linear issues.

# What gets projected

By default, only commitments above a confidence threshold (HIGH). Decisions,
open questions, and blockers can be projected too via explicit kind filters,
but commitments are the natural fit for issue-tracker semantics.

# Mapping

| Verbatim field           | Linear field        |
|--------------------------|---------------------|
| commitment.deliverable   | issue.title         |
| commitment.actor         | issue.assignee      |
| commitment.deadline      | issue.dueDate (if it parses as YYYY-MM-DD or ISO) |
| commitment.sources[*].quote + entity id + source_path | issue.description |
| (env / CLI flag)         | issue.teamId, issue.stateId |

Assignee resolution is best-effort: we look up Linear users by displayName,
name, then email. No match → issue is created unassigned. The actor string is
preserved in the description so it's never lost.

# Idempotency

Every projection writes a row to the `projections` table keyed by
`(entity_id, target_kind, status='active')`. Re-running `verbatim project linear`
on the same DB does not duplicate issues — entities with an active projection
are skipped. `verbatim unproject <id>` flips the projection to inactive
(optionally archiving the Linear issue too).

# Auth

Linear personal API key (`lin_api_…`) passed via `$LINEAR_API_KEY` or
`--api-key`. The key needs `Read` on workspace metadata and `Write` on Issues.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import store

LINEAR_GRAPHQL = "https://api.linear.app/graphql"
TARGET_KIND = "linear_issue"


# ----------------------- GraphQL client -----------------------


class LinearClient:
    """Thin httpx-based GraphQL client for Linear's public API."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = LINEAR_GRAPHQL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Linear API key is required (set LINEAR_API_KEY or pass --api-key).")
        self._api_key = api_key
        self._endpoint = endpoint
        self._owned = client is None
        self._http = client or httpx.Client(
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "User-Agent": "verbatim/0.x",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> LinearClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- queries -----

    def viewer(self) -> dict[str, Any]:
        data = self._request("query { viewer { id name email displayName } }")
        return data["viewer"]

    def list_teams(self) -> list[dict[str, Any]]:
        data = self._request("query { teams { nodes { id name key } } }")
        return data["teams"]["nodes"]

    def list_users(self) -> list[dict[str, Any]]:
        data = self._request(
            "query { users(first: 200) { nodes { id name email displayName active } } }"
        )
        return data["users"]["nodes"]

    def list_workflow_states(self, team_id: str) -> list[dict[str, Any]]:
        data = self._request(
            "query($id: String!) { "
            "  workflowStates(filter: { team: { id: { eq: $id } } }) { "
            "    nodes { id name type position } "
            "  } "
            "}",
            {"id": team_id},
        )
        return data["workflowStates"]["nodes"]

    # ----- mutations -----

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        assignee_id: str | None = None,
        due_date: str | None = None,
        state_id: str | None = None,
    ) -> dict[str, Any]:
        input_dict: dict[str, Any] = {
            "teamId": team_id,
            "title": title[:255],  # Linear enforces a length cap
            "description": description,
        }
        if assignee_id:
            input_dict["assigneeId"] = assignee_id
        if due_date:
            input_dict["dueDate"] = due_date
        if state_id:
            input_dict["stateId"] = state_id

        data = self._request(
            "mutation($input: IssueCreateInput!) { "
            "  issueCreate(input: $input) { "
            "    success "
            "    issue { id identifier url title } "
            "  } "
            "}",
            {"input": input_dict},
        )
        if not data.get("issueCreate", {}).get("success"):
            raise RuntimeError(f"Linear issueCreate returned non-success: {data!r}")
        return data["issueCreate"]["issue"]

    def archive_issue(self, issue_id: str) -> bool:
        data = self._request(
            "mutation($id: String!) { issueArchive(id: $id) { success } }",
            {"id": issue_id},
        )
        return bool(data.get("issueArchive", {}).get("success"))

    # ----- internals -----

    def _request(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._http.post(
            self._endpoint,
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload and payload["errors"]:
            raise RuntimeError(f"Linear GraphQL error: {payload['errors']}")
        return payload.get("data") or {}


# ----------------------- user resolution -----------------------


def build_user_resolver(users: list[dict[str, Any]]) -> callable:
    """Return a function: actor_name → linear_user_id or None.

    Match priority: displayName → name → email local-part. Case-insensitive.
    """
    by_display = {(u.get("displayName") or "").lower(): u["id"] for u in users if u.get("displayName")}
    by_name = {(u.get("name") or "").lower(): u["id"] for u in users if u.get("name")}
    by_email_local = {
        (u.get("email") or "").split("@", 1)[0].lower(): u["id"]
        for u in users
        if u.get("email")
    }

    def resolve(actor: str | None) -> str | None:
        if not actor:
            return None
        key = actor.lower().strip().lstrip("@")
        return by_display.get(key) or by_name.get(key) or by_email_local.get(key)

    return resolve


# ----------------------- entity → Linear issue mapping -----------------------


@dataclass
class IssueDraft:
    title: str
    description: str
    assignee_id: str | None
    due_date: str | None


def render_issue_from_commitment(
    entity: dict[str, Any],
    *,
    assignee_resolver: callable = lambda _: None,
    source_url_prefix: str | None = None,
) -> IssueDraft:
    """Build the Linear issue payload from a Verbatim commitment entity.

    `source_url_prefix` lets you turn `source_label` into a clickable URL when
    your Verbatim deployment has a web UI; if None, the source label is
    rendered as plain text.
    """
    payload = entity.get("payload", {})
    actor = payload.get("actor")
    deliverable = payload.get("deliverable") or "(no deliverable)"
    title = f"{actor}: {deliverable}" if actor else deliverable

    lines: list[str] = []
    lines.append(f"**Verbatim-extracted commitment** ({entity.get('confidence', 'unknown')} confidence)")
    if payload.get("deadline"):
        lines.append(f"Deadline: {payload['deadline']}")
    if payload.get("to"):
        lines.append(f"To: {payload['to']}")
    if payload.get("notes"):
        lines.append(f"Notes: {payload['notes']}")
    lines.append("")
    lines.append("## Supporting quotes")
    for s in entity.get("sources", []):
        speaker = s.get("speaker") or "unknown"
        ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
        lines.append(f"> {ts}{speaker}: {s['verbatim_quote']}")
        if s.get("rationale"):
            lines.append(f"_extractor rationale: {s['rationale']}_")
        lines.append("")
    lines.append(f"_Verbatim entity id: `{entity['id']}`_")

    return IssueDraft(
        title=title,
        description="\n".join(lines).rstrip(),
        assignee_id=assignee_resolver(actor),
        due_date=_parse_deadline_to_iso(payload.get("deadline")),
    )


def _parse_deadline_to_iso(deadline: str | None) -> str | None:
    """Convert a deadline string to YYYY-MM-DD if it parses; None otherwise.

    Conservative on purpose: we don't try to interpret 'EOD Friday' or 'next
    week'. Those are extractor-time guesses the user should refine in Linear.
    """
    if not deadline:
        return None
    s = deadline.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return d.date().isoformat()
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.date().isoformat()
    except ValueError:
        return None


# ----------------------- projection orchestration -----------------------


@dataclass
class ProjectionPlan:
    entity: dict[str, Any]
    draft: IssueDraft
    skip_reason: str | None = None  # set if we'd skip this entity (already projected, etc.)


def plan_projection(
    conn: sqlite3.Connection,
    entity: dict[str, Any],
    *,
    assignee_resolver: callable = lambda _: None,
) -> ProjectionPlan:
    """Decide whether/how to project a single entity. Pure — no API calls."""
    if entity.get("canonical_id") is not None:
        return ProjectionPlan(entity=entity, draft=_empty_draft(), skip_reason="non-canonical (merged sibling)")
    existing = store.find_active_projection(conn, entity_id=entity["id"], target_kind=TARGET_KIND)
    if existing is not None:
        return ProjectionPlan(
            entity=entity,
            draft=_empty_draft(),
            skip_reason=f"already projected as {existing.get('external_id') or existing['id']}",
        )
    draft = render_issue_from_commitment(entity, assignee_resolver=assignee_resolver)
    return ProjectionPlan(entity=entity, draft=draft)


def execute_projection(
    conn: sqlite3.Connection,
    client: LinearClient,
    plan: ProjectionPlan,
    *,
    team_id: str,
    state_id: str | None = None,
) -> dict[str, Any]:
    """Execute a single ProjectionPlan: create the Linear issue, record it."""
    if plan.skip_reason:
        raise RuntimeError(f"Plan marked skip: {plan.skip_reason}")
    issue = client.create_issue(
        team_id=team_id,
        title=plan.draft.title,
        description=plan.draft.description,
        assignee_id=plan.draft.assignee_id,
        due_date=plan.draft.due_date,
        state_id=state_id,
    )
    projection_id = store.insert_projection(
        conn,
        entity_id=plan.entity["id"],
        target_kind=TARGET_KIND,
        external_id=issue.get("id"),
        external_url=issue.get("url"),
        metadata={
            "identifier": issue.get("identifier"),
            "title": issue.get("title"),
            "team_id": team_id,
            "state_id": state_id,
            "assignee_id": plan.draft.assignee_id,
            "due_date": plan.draft.due_date,
        },
    )
    return {
        "projection_id": projection_id,
        "external_id": issue.get("id"),
        "external_url": issue.get("url"),
        "identifier": issue.get("identifier"),
    }


def deactivate_projection(
    conn: sqlite3.Connection,
    projection_id: str,
    *,
    client: LinearClient | None = None,
    close_linear: bool = False,
) -> bool:
    """Mark a projection inactive in our DB; optionally archive the Linear issue too."""
    if close_linear and client is not None:
        # Look up the external id
        proj_rows = conn.execute(
            "SELECT external_id FROM projections WHERE id = ?",
            (projection_id,),
        ).fetchone()
        ext = proj_rows["external_id"] if proj_rows else None
        if ext:
            client.archive_issue(ext)
    return store.update_projection_status(conn, projection_id, "inactive")


def _empty_draft() -> IssueDraft:
    return IssueDraft(title="", description="", assignee_id=None, due_date=None)
