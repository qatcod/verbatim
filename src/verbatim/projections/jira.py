"""Jira projection — push Verbatim commitments out as Jira issues.

Mirrors the Linear / GitHub projections. Auth via Atlassian email + API token
(`https://id.atlassian.com/manage-profile/security/api-tokens`).

# Mapping

| Verbatim field          | Jira field                         |
|-------------------------|-------------------------------------|
| commitment.deliverable  | issue.summary                       |
| commitment.actor        | (preserved in description, no auto-assign — Jira uses accountId, not names) |
| sources[*].verbatim_quote | issue.description (ADF)           |
| entity.id               | embedded in description             |
| (CLI flag) project key  | issue.project.key                   |
| (CLI flag) issue type   | issue.issuetype.name (default Task) |

Description uses Atlassian Document Format (ADF). We emit a minimal ADF
structure with paragraph + blockquote nodes — enough for the quote evidence
to render cleanly without bringing in a full ADF builder library.

# Idempotency
Tracked through the shared `projections` table with `target_kind='jira_issue'`.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import store

TARGET_KIND = "jira_issue"


class JiraClient:
    """Thin httpx wrapper over the Atlassian Jira Cloud REST API v3."""

    def __init__(
        self,
        *,
        site: str,
        email: str,
        api_token: str,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not site:
            raise ValueError("Jira site is required (e.g. https://yourco.atlassian.net).")
        if not email or not api_token:
            raise ValueError("Jira email + API token both required.")
        site = site.rstrip("/")
        if not site.startswith("http"):
            site = f"https://{site}"
        self._site = site
        creds = f"{email}:{api_token}".encode()
        auth_header = "Basic " + base64.b64encode(creds).decode()
        self._owned = client is None
        self._http = client or httpx.Client(
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "verbatim/0.x",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def myself(self) -> dict[str, Any]:
        resp = self._http.get(f"{self._site}/rest/api/3/myself")
        resp.raise_for_status()
        return resp.json()

    def list_projects(self) -> list[dict[str, Any]]:
        resp = self._http.get(f"{self._site}/rest/api/3/project/search")
        resp.raise_for_status()
        return resp.json().get("values", [])

    def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description_adf: dict[str, Any],
        issuetype: str = "Task",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary[:255],
                "issuetype": {"name": issuetype},
                "description": description_adf,
            }
        }
        if labels:
            payload["fields"]["labels"] = labels

        resp = self._http.post(f"{self._site}/rest/api/3/issue", json=payload)
        resp.raise_for_status()
        created = resp.json()
        # Compose the browse URL from key (Jira returns key/id/self)
        key = created.get("key")
        if key:
            created["_browse_url"] = f"{self._site}/browse/{key}"
        return created

    def close_issue(self, *, issue_key: str, transition_name: str = "Done") -> bool:
        # Get available transitions
        resp = self._http.get(f"{self._site}/rest/api/3/issue/{issue_key}/transitions")
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])
        target = next(
            (t for t in transitions if t.get("name", "").lower() == transition_name.lower()),
            None,
        )
        if target is None:
            # Try common alternatives
            target = next(
                (t for t in transitions if t.get("name", "").lower() in
                 {"done", "closed", "resolved", "complete"}),
                None,
            )
        if target is None:
            return False
        resp = self._http.post(
            f"{self._site}/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": target["id"]}},
        )
        return resp.status_code in (204, 200)


# ----- Atlassian Document Format builder (minimal) -----


def adf_paragraph(text: str) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text}] if text else [],
    }


def adf_quote(text: str) -> dict[str, Any]:
    return {
        "type": "blockquote",
        "content": [adf_paragraph(text)],
    }


def adf_doc(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "type": "doc", "content": blocks}


# ----- entity → Jira issue draft -----


@dataclass
class IssueDraft:
    summary: str
    description_adf: dict[str, Any]
    labels: list[str] = field(default_factory=list)


def render_issue_from_commitment(
    entity: dict[str, Any],
    *,
    extra_labels: list[str] | None = None,
) -> IssueDraft:
    payload = entity.get("payload", {})
    actor = payload.get("actor")
    deliverable = payload.get("deliverable") or "(no deliverable)"
    summary = f"{actor}: {deliverable}" if actor else deliverable

    blocks: list[dict[str, Any]] = [
        adf_paragraph(
            f"Verbatim-extracted commitment "
            f"({entity.get('confidence', 'unknown')} confidence)."
        ),
    ]
    if payload.get("deadline"):
        blocks.append(adf_paragraph(f"Deadline: {payload['deadline']}"))
    if payload.get("to"):
        blocks.append(adf_paragraph(f"To: {payload['to']}"))
    if payload.get("notes"):
        blocks.append(adf_paragraph(f"Notes: {payload['notes']}"))

    blocks.append(adf_paragraph("Supporting quotes:"))
    for s in entity.get("sources", []):
        speaker = s.get("speaker") or "unknown"
        ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
        blocks.append(adf_quote(f"{ts}{speaker}: {s['verbatim_quote']}"))

    blocks.append(adf_paragraph(f"Verbatim entity id: {entity['id']}"))

    labels = ["verbatim", *(extra_labels or [])]

    return IssueDraft(summary=summary, description_adf=adf_doc(blocks), labels=labels)


# ----- projection orchestration -----


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
            draft=IssueDraft("", adf_doc([])),
            skip_reason="non-canonical (merged sibling)",
        )
    existing = store.find_active_projection(
        conn, entity_id=entity["id"], target_kind=TARGET_KIND
    )
    if existing is not None:
        meta = existing.get("metadata") or {}
        return ProjectionPlan(
            entity=entity,
            draft=IssueDraft("", adf_doc([])),
            skip_reason=f"already projected as {meta.get('key') or existing.get('external_id')}",
        )
    return ProjectionPlan(
        entity=entity,
        draft=render_issue_from_commitment(entity, extra_labels=extra_labels),
    )


def execute_projection(
    conn: sqlite3.Connection,
    client: JiraClient,
    plan: ProjectionPlan,
    *,
    project_key: str,
    issuetype: str = "Task",
) -> dict[str, Any]:
    if plan.skip_reason:
        raise RuntimeError(f"Plan marked skip: {plan.skip_reason}")
    d = plan.draft
    issue = client.create_issue(
        project_key=project_key,
        summary=d.summary,
        description_adf=d.description_adf,
        issuetype=issuetype,
        labels=d.labels,
    )
    projection_id = store.insert_projection(
        conn,
        entity_id=plan.entity["id"],
        target_kind=TARGET_KIND,
        external_id=str(issue.get("id") or ""),
        external_url=issue.get("_browse_url"),
        metadata={
            "key": issue.get("key"),
            "summary": d.summary,
            "project_key": project_key,
            "issuetype": issuetype,
            "labels": d.labels,
        },
    )
    return {
        "projection_id": projection_id,
        "external_id": str(issue.get("id") or ""),
        "external_url": issue.get("_browse_url"),
        "key": issue.get("key"),
    }


def deactivate_projection(
    conn: sqlite3.Connection,
    projection_id: str,
    *,
    client: JiraClient | None = None,
    close_external: bool = False,
) -> bool:
    if close_external and client is not None:
        row = conn.execute(
            "SELECT metadata_json FROM projections WHERE id = ?",
            (projection_id,),
        ).fetchone()
        if row:
            meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            key = meta.get("key")
            if key:
                try:
                    client.close_issue(issue_key=key)
                except Exception:  # noqa: BLE001
                    pass
    return store.update_projection_status(conn, projection_id, "inactive")
