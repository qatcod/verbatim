"""Verbatim web UI — read-mostly Starlette app for browsing state.

The third consumer surface (after Slack bot and MCP). Open in a browser at
the local URL and you get a clickable view of the state graph: list pages
per entity kind, full detail with source quotes, sessions, projections.

# Design

- Read-only for v0.6. Resolve / link / unlink stay on the CLI. (Mutating
  HTTP endpoints with no auth are a footgun on a multi-user host.)
- Starlette + hand-rolled HTML via f-strings. No build step, no Jinja
  files to package, no React. Adds two deps (`starlette`, `uvicorn`)
  that mcp's transitive tree already pulled in.
- Default bind is 127.0.0.1. Multi-user / SSO / token auth is the v0.7
  story; for now treat this as a local browser tool.
- Every user-supplied string runs through `html.escape`. No exceptions.

# Routes

  GET  /                       dashboard
  GET  /commitments            list, filterable by ?actor= ?min_confidence= ?ungrouped=
  GET  /decisions              list
  GET  /open-questions         list
  GET  /blockers               list
  GET  /sessions               recent extraction sessions
  GET  /projections            active projections to external trackers
  GET  /entity/{id}            full detail with all source quotes
"""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from . import state, store

# ----------------------- DB lifecycle for handlers -----------------------


class _AppState:
    """Singleton holding the resolved DB path. Set at app creation, used by handlers."""

    db_path: Path | None = None


def _open_conn() -> sqlite3.Connection:
    return state.open_db(_AppState.db_path)


# ----------------------- HTML shell -----------------------


_NAV_LINKS = [
    ("/", "Dashboard"),
    ("/commitments", "Commitments"),
    ("/decisions", "Decisions"),
    ("/open-questions", "Questions"),
    ("/blockers", "Blockers"),
    ("/sessions", "Sessions"),
    ("/projections", "Projections"),
]


_CSS = """
:root {
  --bg: #fafafa;
  --fg: #1a1a1a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
  --green: #16a34a;
  --yellow: #ca8a04;
  --red: #dc2626;
  --card: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
header { background: var(--card); border-bottom: 1px solid var(--border); padding: 0 24px; }
nav { display: flex; gap: 4px; flex-wrap: wrap; padding: 12px 0; }
nav a {
  color: var(--fg); text-decoration: none; padding: 6px 12px;
  border-radius: 6px; font-size: 14px;
}
nav a:hover { background: var(--bg); }
nav a.active { background: var(--accent); color: #fff; }
main { max-width: 1100px; margin: 32px auto; padding: 0 24px; }
h1 { font-size: 24px; margin: 0 0 16px; font-weight: 600; }
h2 { font-size: 18px; margin: 28px 0 12px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; vertical-align: top; }
th { background: var(--bg); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); }
tr:last-child td { border-bottom: none; }
tr:hover { background: #f9fafb; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; font-weight: 500; }
.badge.high { background: #dcfce7; color: var(--green); }
.badge.medium { background: #fef3c7; color: var(--yellow); }
.badge.low { background: #fee2e2; color: var(--red); }
.pill { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); margin-left: 6px; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 13px; color: var(--muted); }
.muted { color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 24px 0; }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.stat-card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-card .value { font-size: 28px; font-weight: 600; margin-top: 4px; }
.quote {
  border-left: 3px solid var(--border); padding: 6px 0 6px 12px; margin: 8px 0;
  color: var(--fg); font-size: 14px;
}
.quote .speaker { font-weight: 600; }
.quote .ts { color: var(--muted); font-family: ui-monospace, Menlo, monospace; font-size: 12px; margin-right: 6px; }
.quote .rationale { color: var(--muted); font-size: 12px; font-style: italic; margin-top: 4px; }
.entity-detail { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 24px; }
.entity-detail dl { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; margin: 16px 0; }
.entity-detail dt { color: var(--muted); font-size: 13px; }
.entity-detail dd { margin: 0; font-size: 14px; }
.empty { text-align: center; padding: 48px; color: var(--muted); }
form.inline { display: inline; }
.filters { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; align-items: center; }
.filters label { font-size: 13px; color: var(--muted); }
.filters input, .filters select {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px; background: var(--card);
}
.filters button {
  padding: 6px 14px; background: var(--accent); color: #fff;
  border: none; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.filters button:hover { opacity: 0.9; }
"""


def _shell(title: str, body: str, active: str = "") -> str:
    nav_html = " ".join(
        f'<a href="{html.escape(path)}"'
        f' class="{"active" if path == active else ""}">{html.escape(label)}</a>'
        for path, label in _NAV_LINKS
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Verbatim</title>
<style>{_CSS}</style>
</head>
<body>
<header><nav>{nav_html}</nav></header>
<main>{body}</main>
</body>
</html>"""


def _confidence_badge(conf: str) -> str:
    label = html.escape(conf)
    return f'<span class="badge {label}">{label}</span>'


def _entity_link(entity_id: str, *, label: str | None = None) -> str:
    safe_id = html.escape(entity_id)
    text = html.escape(label or (entity_id[:8] + "…"))
    return f'<a href="/entity/{safe_id}" class="mono">{text}</a>'


def _maybe(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else '<span class="muted">—</span>'


# ----------------------- routes -----------------------


async def home(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        stats_dict = state.stats(conn)
        recent_sessions = state.recent_sessions(conn, limit=5)
        recent_commitments = state.list_commitments(conn, limit=5)
    finally:
        conn.close()

    stat_cards = [
        ("Sessions", stats_dict.get("sessions", 0)),
        ("Commitments", stats_dict.get("commitments_open", 0)),
        ("Decisions", stats_dict.get("decisions_open", 0)),
        ("Questions", stats_dict.get("open_questions_open", 0)),
        ("Blockers", stats_dict.get("blockers_open", 0)),
        ("Merged", stats_dict.get("entities_merged", 0)),
        ("Projections", stats_dict.get("projections_active", 0)),
    ]
    stats_html = "".join(
        f'<div class="stat-card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{n}</div></div>'
        for label, n in stat_cards
    )

    if recent_commitments:
        commits_rows = "".join(_render_commitment_row(c) for c in recent_commitments)
        commits_html = f"""<h2>Recent commitments</h2>
<table>
<thead><tr><th>Actor</th><th>Deliverable</th><th>Deadline</th><th>Confidence</th><th>ID</th></tr></thead>
<tbody>{commits_rows}</tbody></table>"""
    else:
        commits_html = '<h2>Recent commitments</h2><div class="empty">No commitments yet — try <span class="mono">verbatim ingest</span>.</div>'

    if recent_sessions:
        session_rows = "".join(_render_session_row(s) for s in recent_sessions)
        sessions_html = f"""<h2>Recent sessions</h2>
<table>
<thead><tr><th>When (UTC)</th><th>Source</th><th>Kind</th><th>Items</th></tr></thead>
<tbody>{session_rows}</tbody></table>"""
    else:
        sessions_html = ""

    body = (
        f'<h1>Verbatim dashboard</h1>'
        f'<div class="stats">{stats_html}</div>'
        f'{commits_html}'
        f'{sessions_html}'
    )
    return HTMLResponse(_shell("Dashboard", body, active="/"))


def _render_commitment_row(c: dict[str, Any]) -> str:
    p = c["payload"]
    merged_pill = (
        f'<span class="pill">+{c["merged_count"]} merged</span>' if c.get("merged_count") else ""
    )
    return (
        "<tr>"
        f"<td>{_maybe(p.get('actor'))}</td>"
        f"<td>{_maybe(p.get('deliverable'))}{merged_pill}</td>"
        f"<td>{_maybe(p.get('deadline'))}</td>"
        f"<td>{_confidence_badge(c['confidence'])}</td>"
        f"<td>{_entity_link(c['id'])}</td>"
        "</tr>"
    )


def _render_session_row(s: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td class='mono'>{html.escape(s['extracted_at'][:19])}</td>"
        f"<td>{_maybe(s.get('source_path') or '<stdin>')}</td>"
        f"<td>{_maybe(s.get('source_kind'))}</td>"
        f"<td>{s.get('entity_count', 0)}</td>"
        "</tr>"
    )


async def commitments(request: Request) -> HTMLResponse:
    actor = request.query_params.get("actor")
    min_conf = request.query_params.get("min_confidence")
    ungrouped = request.query_params.get("ungrouped") in ("1", "true", "yes")

    conn = _open_conn()
    try:
        items = state.list_commitments(
            conn, actor=actor, min_confidence=min_conf,
            canonical_only=not ungrouped, limit=200,
        )
    finally:
        conn.close()

    filters_form = _filter_form(
        path="/commitments",
        fields=[
            ("actor", "Actor", actor or ""),
            ("min_confidence", "Min confidence", min_conf or "",
             ["", "low", "medium", "high"]),
        ],
        ungrouped=ungrouped,
    )

    if not items:
        body = (
            f"<h1>Commitments</h1>{filters_form}"
            '<div class="empty">No commitments match.</div>'
        )
    else:
        rows = "".join(_render_commitment_row(c) for c in items)
        body = (
            f"<h1>Commitments <span class='muted'>({len(items)})</span></h1>"
            f"{filters_form}"
            f"<table><thead><tr><th>Actor</th><th>Deliverable</th>"
            f"<th>Deadline</th><th>Confidence</th><th>ID</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Commitments", body, active="/commitments"))


async def decisions(request: Request) -> HTMLResponse:
    min_conf = request.query_params.get("min_confidence")
    ungrouped = request.query_params.get("ungrouped") in ("1", "true", "yes")
    conn = _open_conn()
    try:
        items = state.list_decisions(
            conn, min_confidence=min_conf,
            canonical_only=not ungrouped, limit=200,
        )
    finally:
        conn.close()

    filters_form = _filter_form(
        "/decisions",
        [("min_confidence", "Min confidence", min_conf or "",
          ["", "low", "medium", "high"])],
        ungrouped=ungrouped,
    )

    if not items:
        body = f"<h1>Decisions</h1>{filters_form}<div class='empty'>No decisions match.</div>"
    else:
        rows = "".join(_render_decision_row(d) for d in items)
        body = (
            f"<h1>Decisions <span class='muted'>({len(items)})</span></h1>"
            f"{filters_form}"
            f"<table><thead><tr><th>Topic</th><th>Outcome</th>"
            f"<th>Participants</th><th>Confidence</th><th>ID</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Decisions", body, active="/decisions"))


def _render_decision_row(d: dict[str, Any]) -> str:
    p = d["payload"]
    merged_pill = f'<span class="pill">+{d["merged_count"]} merged</span>' if d.get("merged_count") else ""
    participants = ", ".join(p.get("participants") or [])
    return (
        "<tr>"
        f"<td>{_maybe(p.get('topic'))}{merged_pill}</td>"
        f"<td>{_maybe(p.get('outcome'))}</td>"
        f"<td>{_maybe(participants)}</td>"
        f"<td>{_confidence_badge(d['confidence'])}</td>"
        f"<td>{_entity_link(d['id'])}</td>"
        "</tr>"
    )


async def open_questions(request: Request) -> HTMLResponse:
    raised_by = request.query_params.get("raised_by")
    min_conf = request.query_params.get("min_confidence")
    ungrouped = request.query_params.get("ungrouped") in ("1", "true", "yes")
    conn = _open_conn()
    try:
        items = state.list_open_questions(
            conn, raised_by=raised_by, min_confidence=min_conf,
            canonical_only=not ungrouped, limit=200,
        )
    finally:
        conn.close()

    filters_form = _filter_form(
        "/open-questions",
        [
            ("raised_by", "Raised by", raised_by or ""),
            ("min_confidence", "Min confidence", min_conf or "",
             ["", "low", "medium", "high"]),
        ],
        ungrouped=ungrouped,
    )

    if not items:
        body = f"<h1>Open questions</h1>{filters_form}<div class='empty'>No open questions match.</div>"
    else:
        rows = "".join(_render_question_row(q) for q in items)
        body = (
            f"<h1>Open questions <span class='muted'>({len(items)})</span></h1>"
            f"{filters_form}"
            f"<table><thead><tr><th>Topic</th><th>Question</th>"
            f"<th>Raised by</th><th>Confidence</th><th>ID</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Open questions", body, active="/open-questions"))


def _render_question_row(q: dict[str, Any]) -> str:
    p = q["payload"]
    merged_pill = f'<span class="pill">+{q["merged_count"]} merged</span>' if q.get("merged_count") else ""
    return (
        "<tr>"
        f"<td>{_maybe(p.get('topic'))}{merged_pill}</td>"
        f"<td>{_maybe(p.get('question'))}</td>"
        f"<td>{_maybe(p.get('raised_by'))}</td>"
        f"<td>{_confidence_badge(q['confidence'])}</td>"
        f"<td>{_entity_link(q['id'])}</td>"
        "</tr>"
    )


async def blockers(request: Request) -> HTMLResponse:
    owner = request.query_params.get("owner")
    min_conf = request.query_params.get("min_confidence")
    ungrouped = request.query_params.get("ungrouped") in ("1", "true", "yes")
    conn = _open_conn()
    try:
        items = state.list_blockers(
            conn, owner=owner, min_confidence=min_conf,
            canonical_only=not ungrouped, limit=200,
        )
    finally:
        conn.close()

    filters_form = _filter_form(
        "/blockers",
        [
            ("owner", "Owner", owner or ""),
            ("min_confidence", "Min confidence", min_conf or "",
             ["", "low", "medium", "high"]),
        ],
        ungrouped=ungrouped,
    )

    if not items:
        body = f"<h1>Blockers</h1>{filters_form}<div class='empty'>No blockers match.</div>"
    else:
        rows = "".join(_render_blocker_row(b) for b in items)
        body = (
            f"<h1>Blockers <span class='muted'>({len(items)})</span></h1>"
            f"{filters_form}"
            f"<table><thead><tr><th>Blocked thing</th><th>Blocked by</th>"
            f"<th>Owner</th><th>Confidence</th><th>ID</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Blockers", body, active="/blockers"))


def _render_blocker_row(b: dict[str, Any]) -> str:
    p = b["payload"]
    merged_pill = f'<span class="pill">+{b["merged_count"]} merged</span>' if b.get("merged_count") else ""
    return (
        "<tr>"
        f"<td>{_maybe(p.get('blocked_thing'))}{merged_pill}</td>"
        f"<td>{_maybe(p.get('blocked_by'))}</td>"
        f"<td>{_maybe(p.get('owner'))}</td>"
        f"<td>{_confidence_badge(b['confidence'])}</td>"
        f"<td>{_entity_link(b['id'])}</td>"
        "</tr>"
    )


async def sessions(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        items = state.recent_sessions(conn, limit=100)
    finally:
        conn.close()
    if not items:
        body = "<h1>Sessions</h1><div class='empty'>No extraction sessions yet.</div>"
    else:
        rows = "".join(_render_session_row(s) for s in items)
        body = (
            f"<h1>Sessions <span class='muted'>({len(items)})</span></h1>"
            f"<table><thead><tr><th>When (UTC)</th><th>Source</th><th>Kind</th>"
            f"<th>Items</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Sessions", body, active="/sessions"))


async def projections(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        items = store.list_projections(conn, status="active", limit=200)
    finally:
        conn.close()
    if not items:
        body = "<h1>Projections</h1><div class='empty'>No active projections.</div>"
    else:
        rows = "".join(_render_projection_row(p) for p in items)
        body = (
            f"<h1>Active projections <span class='muted'>({len(items)})</span></h1>"
            f"<table><thead><tr><th>Entity</th><th>Target</th><th>Identifier</th>"
            f"<th>URL</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    return HTMLResponse(_shell("Projections", body, active="/projections"))


def _render_projection_row(p: dict[str, Any]) -> str:
    meta = p.get("metadata") or {}
    identifier = meta.get("identifier") or (p.get("external_id") or "")[:12]
    url = p.get("external_url") or ""
    url_html = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(url[:50])}</a>' if url else '<span class="muted">—</span>'
    return (
        "<tr>"
        f"<td>{_entity_link(p['entity_id'], label=p.get('primary_topic') or p['entity_id'])}</td>"
        f"<td>{_maybe(p.get('target_kind'))}</td>"
        f"<td class='mono'>{_maybe(identifier)}</td>"
        f"<td>{url_html}</td>"
        f"<td class='mono'>{html.escape(p['created_at'][:19])}</td>"
        "</tr>"
    )


async def entity_detail(request: Request) -> HTMLResponse:
    entity_id = request.path_params["entity_id"]
    conn = _open_conn()
    try:
        entity = state.show_entity(conn, entity_id)
    finally:
        conn.close()
    if entity is None:
        body = f"<h1>Entity not found</h1><div class='empty'>No entity with id <span class='mono'>{html.escape(entity_id)}</span>.</div>"
        return HTMLResponse(_shell("Not found", body), status_code=404)

    payload = entity["payload"]
    rows: list[tuple[str, str]] = [("kind", html.escape(entity["kind"]))]
    rows.append(("confidence", _confidence_badge(entity["confidence"])))
    rows.append(("status", html.escape(entity["status"])))
    rows.append(("created", html.escape(entity["created_at"][:19])))
    if entity.get("merged_count"):
        rows.append(("merged from", f"{entity['merged_count']+1} sources"))
    if entity.get("canonical_id"):
        rows.append(("merged into", _entity_link(entity["canonical_id"])))

    if entity["kind"] == "commitment":
        rows.append(("actor", _maybe(payload.get("actor"))))
        rows.append(("deliverable", _maybe(payload.get("deliverable"))))
        if payload.get("deadline"):
            rows.append(("deadline", _maybe(payload.get("deadline"))))
        if payload.get("to"):
            rows.append(("to", _maybe(payload.get("to"))))
        if payload.get("notes"):
            rows.append(("notes", _maybe(payload.get("notes"))))
    elif entity["kind"] == "decision":
        rows.append(("topic", _maybe(payload.get("topic"))))
        rows.append(("outcome", _maybe(payload.get("outcome"))))
        if payload.get("participants"):
            rows.append(("participants", html.escape(", ".join(payload["participants"]))))
        if payload.get("rationale"):
            rows.append(("rationale", _maybe(payload.get("rationale"))))
        if payload.get("alternatives_considered"):
            rows.append(("alternatives", html.escape(", ".join(payload["alternatives_considered"]))))
    elif entity["kind"] == "open_question":
        rows.append(("topic", _maybe(payload.get("topic"))))
        rows.append(("question", _maybe(payload.get("question"))))
        if payload.get("raised_by"):
            rows.append(("raised by", _maybe(payload.get("raised_by"))))
        if payload.get("addressed_to"):
            rows.append(("addressed to", _maybe(payload.get("addressed_to"))))
    elif entity["kind"] == "blocker":
        rows.append(("blocked thing", _maybe(payload.get("blocked_thing"))))
        rows.append(("blocked by", _maybe(payload.get("blocked_by"))))
        if payload.get("owner"):
            rows.append(("owner", _maybe(payload.get("owner"))))

    dl_rows = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows)

    quotes_html = []
    for s in entity.get("sources", []):
        ts = f'<span class="ts">[{html.escape(s["approximate_timestamp"])}]</span> ' if s.get("approximate_timestamp") else ""
        speaker = f'<span class="speaker">{html.escape(s["speaker"])}:</span> ' if s.get("speaker") else ""
        rationale = (
            f'<div class="rationale">{html.escape(s["rationale"])}</div>'
            if s.get("rationale") else ""
        )
        quotes_html.append(
            f'<div class="quote">{ts}{speaker}{html.escape(s["verbatim_quote"])}{rationale}</div>'
        )
    quotes_block = "".join(quotes_html) or '<div class="empty">No sources.</div>'

    body = (
        f"<h1>{html.escape(entity['kind'])} <span class='mono muted'>{html.escape(entity_id)}</span></h1>"
        f"<div class='entity-detail'>"
        f"<dl>{dl_rows}</dl>"
        f"<h2>Sources ({len(entity.get('sources', []))})</h2>"
        f"{quotes_block}"
        f"</div>"
    )
    return HTMLResponse(_shell("Entity", body))


# ----------------------- filter form helper -----------------------


def _filter_form(
    path: str,
    fields: list[tuple],
    *,
    ungrouped: bool,
) -> str:
    """Build a small inline filter form for list pages."""
    parts: list[str] = [f'<form class="filters" method="get" action="{html.escape(path)}">']
    for spec in fields:
        if len(spec) == 4:
            name, label, value, options = spec
            opt_html = "".join(
                f'<option value="{html.escape(o)}"'
                f' {"selected" if o == value else ""}>{html.escape(o or "any")}</option>'
                for o in options
            )
            parts.append(
                f'<label>{html.escape(label)}: '
                f'<select name="{html.escape(name)}">{opt_html}</select></label>'
            )
        else:
            name, label, value = spec
            parts.append(
                f'<label>{html.escape(label)}: '
                f'<input type="text" name="{html.escape(name)}" '
                f'value="{html.escape(value)}" /></label>'
            )
    checked = "checked" if ungrouped else ""
    parts.append(
        f'<label><input type="checkbox" name="ungrouped" value="1" {checked}> Show merged siblings separately</label>'
    )
    parts.append('<button type="submit">Filter</button></form>')
    return "".join(parts)


# ----------------------- app factory -----------------------


def create_app(db_path: Path | None = None) -> Starlette:
    """Build the Starlette app. The db_path resolves at handler time."""
    _AppState.db_path = db_path
    routes = [
        Route("/", home),
        Route("/commitments", commitments),
        Route("/decisions", decisions),
        Route("/open-questions", open_questions),
        Route("/blockers", blockers),
        Route("/sessions", sessions),
        Route("/projections", projections),
        Route("/entity/{entity_id}", entity_detail),
    ]
    return Starlette(routes=routes)
