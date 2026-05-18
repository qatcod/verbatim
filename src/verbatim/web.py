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


# Inline SVG icons — no external icon font, no CDN. Keep these terse; they
# render at 18×18 by default and inherit `currentColor`.
_ICONS: dict[str, str] = {
    "dashboard": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="3" y="3" width="7" height="9" rx="1"/>'
        '<rect x="14" y="3" width="7" height="5" rx="1"/>'
        '<rect x="14" y="12" width="7" height="9" rx="1"/>'
        '<rect x="3" y="16" width="7" height="5" rx="1"/></svg>'
    ),
    "commitments": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M9 11l3 3L22 4"/>'
        '<path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
    ),
    "decisions": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>'
    ),
    "questions": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="10"/>'
        '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
        '<line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
    ),
    "blockers": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="10"/>'
        '<line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'
    ),
    "sessions": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
    ),
    "projections": (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M5 12h14"/><polyline points="12 5 19 12 12 19"/></svg>'
    ),
    "external": (
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
        '<polyline points="15 3 21 3 21 9"/>'
        '<line x1="10" y1="14" x2="21" y2="3"/></svg>'
    ),
    "logo": (
        '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3 6h18"/><path d="M3 12h12"/><path d="M3 18h6"/>'
        '<circle cx="19" cy="17" r="3" fill="currentColor" stroke="none"/></svg>'
    ),
}


_NAV_LINKS = [
    ("/", "Dashboard", "dashboard"),
    ("/commitments", "Commitments", "commitments"),
    ("/decisions", "Decisions", "decisions"),
    ("/open-questions", "Questions", "questions"),
    ("/blockers", "Blockers", "blockers"),
    ("/sessions", "Sessions", "sessions"),
    ("/projections", "Projections", "projections"),
]


_CSS = """
/* ── reset + theme tokens ───────────────────────────────────────────── */
:root {
  color-scheme: dark;
  --bg: #0a0a0c;
  --surface: #131316;
  --surface-2: #1a1a1f;
  --surface-3: #232329;
  --border: #2a2a32;
  --border-strong: #3a3a44;
  --fg: #f4f4f5;
  --fg-2: #c4c4c9;
  --muted: #8b8b93;
  --muted-2: #6b6b73;
  --accent: #a78bfa;
  --accent-2: #8b5cf6;
  --accent-bg: rgba(139, 92, 246, 0.12);
  --green: #34d399;
  --green-bg: rgba(52, 211, 153, 0.12);
  --yellow: #fbbf24;
  --yellow-bg: rgba(251, 191, 36, 0.12);
  --red: #f87171;
  --red-bg: rgba(248, 113, 113, 0.12);
  --shadow-sm: 0 1px 2px 0 rgba(0,0,0,0.4);
  --shadow-md: 0 8px 24px -8px rgba(0,0,0,0.6);
  --radius: 10px;
  --radius-sm: 6px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background:
    radial-gradient(ellipse 80% 50% at 50% -10%, rgba(139, 92, 246, 0.08), transparent),
    var(--bg);
  color: var(--fg);
  font-family: 'Inter', system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}
::selection { background: var(--accent-bg); color: var(--accent); }

/* ── layout: sidebar + main ─────────────────────────────────────────── */
.app {
  display: grid;
  grid-template-columns: 232px 1fr;
  min-height: 100vh;
}
aside.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  padding: 20px 12px;
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 8px 20px; color: var(--accent);
}
.brand .name {
  font-weight: 700; font-size: 16px; letter-spacing: -0.01em;
  color: var(--fg);
}
.brand .badge-version {
  margin-left: auto; font-size: 10px; color: var(--muted);
  border: 1px solid var(--border); padding: 2px 6px; border-radius: 4px;
  font-family: ui-monospace, Menlo, monospace;
}
nav.side { display: flex; flex-direction: column; gap: 1px; }
nav.side .group-label {
  font-size: 11px; color: var(--muted-2); text-transform: uppercase;
  letter-spacing: 0.08em; padding: 14px 10px 6px;
}
nav.side a {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px;
  color: var(--fg-2); text-decoration: none;
  border-radius: var(--radius-sm);
  font-size: 13.5px; font-weight: 500;
  transition: background 0.12s ease, color 0.12s ease;
}
nav.side a:hover { background: var(--surface-2); color: var(--fg); }
nav.side a.active {
  background: var(--accent-bg); color: var(--accent);
}
nav.side a.active svg { color: var(--accent); }
nav.side a svg { color: var(--muted); flex-shrink: 0; }
nav.side a:hover svg { color: var(--fg-2); }

main {
  padding: 32px 40px 80px;
  max-width: 1280px;
  width: 100%;
}
header.page {
  margin-bottom: 24px;
  display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
}
header.page h1 {
  font-size: 22px; font-weight: 600; margin: 0;
  letter-spacing: -0.01em;
}
header.page .subtitle {
  color: var(--muted); font-size: 13px; margin-left: 12px;
}
h2.section {
  font-size: 13px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted);
  margin: 32px 0 12px;
}
h2.section:first-child { margin-top: 0; }

/* ── stat cards ─────────────────────────────────────────────────────── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 24px;
}
.stat {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  position: relative;
  transition: border-color 0.12s ease, transform 0.12s ease;
}
.stat:hover { border-color: var(--border-strong); }
.stat .stat-label {
  color: var(--muted); font-size: 11.5px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.06em;
  display: flex; align-items: center; gap: 6px;
}
.stat .stat-value {
  font-size: 28px; font-weight: 600; margin-top: 6px;
  letter-spacing: -0.02em; color: var(--fg);
}
.stat .stat-icon {
  position: absolute; top: 14px; right: 14px;
  width: 32px; height: 32px; border-radius: 8px;
  background: var(--accent-bg); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
}
.stat.muted .stat-icon { background: var(--surface-3); color: var(--muted); }

/* ── cards (table replacements, content blocks) ─────────────────────── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.card-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px;
  background: linear-gradient(180deg, var(--surface-2) 0%, var(--surface) 100%);
}
.card-header h3 {
  margin: 0; font-size: 14px; font-weight: 600;
}
.card-header .count {
  color: var(--muted); font-size: 13px;
  background: var(--surface-3); padding: 2px 8px; border-radius: 9999px;
}

/* ── tables ─────────────────────────────────────────────────────────── */
table {
  width: 100%; border-collapse: collapse;
}
th, td {
  padding: 12px 18px; text-align: left;
  border-bottom: 1px solid var(--border);
  font-size: 13.5px; vertical-align: middle;
}
th {
  background: var(--surface-2);
  font-weight: 500; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
}
tbody tr { transition: background 0.08s ease; }
tbody tr:hover { background: var(--surface-2); }
tbody tr:last-child td { border-bottom: none; }
td.primary { color: var(--fg); font-weight: 500; }
td.subtle { color: var(--muted); }

/* ── links + buttons ────────────────────────────────────────────────── */
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-2); text-decoration: underline; text-underline-offset: 3px; }
a.entity-ref {
  font-family: ui-monospace, Menlo, monospace; font-size: 12.5px;
  color: var(--muted); padding: 2px 6px; border-radius: 4px;
  background: var(--surface-3); border: 1px solid var(--border);
  text-decoration: none; transition: all 0.12s ease;
}
a.entity-ref:hover {
  color: var(--accent); border-color: var(--accent-bg);
  background: var(--accent-bg); text-decoration: none;
}

/* ── badges + pills ─────────────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 9999px;
  font-size: 11.5px; font-weight: 600;
  letter-spacing: 0.01em;
}
.badge.high { background: var(--green-bg); color: var(--green); }
.badge.medium { background: var(--yellow-bg); color: var(--yellow); }
.badge.low { background: var(--red-bg); color: var(--red); }
.badge::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%;
  background: currentColor;
}
.pill {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 4px;
  font-size: 11px; color: var(--muted);
  background: var(--surface-3); border: 1px solid var(--border);
  margin-left: 8px;
}

/* ── monospace + muted text ─────────────────────────────────────────── */
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; color: var(--muted); }
.muted { color: var(--muted); }
.dim { color: var(--muted-2); }

/* ── entity detail ──────────────────────────────────────────────────── */
.entity-detail {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 28px 32px;
}
.entity-detail .entity-meta {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 20px;
}
.entity-detail dl {
  display: grid; grid-template-columns: 150px 1fr; gap: 12px 24px;
  margin: 20px 0; padding: 16px 20px;
  background: var(--surface-2); border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.entity-detail dt {
  color: var(--muted); font-size: 12.5px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.entity-detail dd { margin: 0; color: var(--fg); font-size: 14px; }

/* ── quote blocks ───────────────────────────────────────────────────── */
.quote {
  position: relative;
  padding: 14px 16px 14px 20px;
  margin: 10px 0;
  background: var(--surface-2);
  border-left: 3px solid var(--accent-2);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 14px; color: var(--fg);
}
.quote .speaker {
  font-weight: 600; color: var(--accent); margin-right: 4px;
}
.quote .ts {
  display: inline-block; font-family: ui-monospace, Menlo, monospace;
  font-size: 11.5px; color: var(--muted); margin-right: 8px;
  background: var(--surface-3); padding: 1px 6px; border-radius: 3px;
}
.quote .rationale {
  margin-top: 8px; font-size: 12.5px; color: var(--muted);
  font-style: italic;
  padding-top: 8px; border-top: 1px dashed var(--border);
}

/* ── filters ────────────────────────────────────────────────────────── */
.filters {
  display: flex; gap: 12px; flex-wrap: wrap; align-items: center;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px; margin-bottom: 16px;
}
.filters label {
  display: flex; align-items: center; gap: 8px;
  font-size: 12.5px; color: var(--muted); font-weight: 500;
}
.filters input[type="text"], .filters select {
  padding: 6px 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 13px; color: var(--fg);
  font-family: inherit;
  transition: border-color 0.12s ease;
}
.filters input[type="text"]:focus, .filters select:focus {
  outline: none; border-color: var(--accent);
}
.filters input[type="checkbox"] {
  accent-color: var(--accent);
}
.filters button {
  padding: 6px 16px;
  background: var(--accent-2); color: #fff;
  border: 1px solid var(--accent-2);
  border-radius: var(--radius-sm);
  font-size: 13px; font-weight: 500; cursor: pointer;
  transition: background 0.12s ease;
}
.filters button:hover { background: var(--accent); }

/* ── empty states ───────────────────────────────────────────────────── */
.empty {
  text-align: center; padding: 64px 32px;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--muted);
}
.empty .empty-title {
  font-size: 16px; font-weight: 600; color: var(--fg-2);
  margin-bottom: 8px;
}
.empty .empty-hint {
  font-size: 13px; color: var(--muted); margin-bottom: 16px;
}
.empty code {
  display: inline-block;
  background: var(--surface-3); padding: 6px 12px;
  border-radius: var(--radius-sm); border: 1px solid var(--border);
  font-family: ui-monospace, Menlo, monospace; font-size: 12.5px;
  color: var(--accent); margin-top: 4px;
}

/* ── responsive ─────────────────────────────────────────────────────── */
@media (max-width: 860px) {
  .app { grid-template-columns: 1fr; }
  aside.sidebar { position: static; height: auto; padding: 12px; }
  nav.side { flex-direction: row; flex-wrap: wrap; gap: 4px; }
  nav.side .group-label { display: none; }
  nav.side a { padding: 6px 10px; }
  main { padding: 20px 16px 60px; }
  .entity-detail dl { grid-template-columns: 1fr; }
}
"""


def _shell(title: str, body: str, active: str = "") -> str:
    nav_items = []
    for path, label, icon in _NAV_LINKS:
        is_active = "active" if path == active else ""
        icon_svg = _ICONS.get(icon, "")
        nav_items.append(
            f'<a href="{html.escape(path)}" class="{is_active}">'
            f"{icon_svg}<span>{html.escape(label)}</span></a>"
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
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      {_ICONS["logo"]}
      <span class="name">Verbatim</span>
      <span class="badge-version">v0.6</span>
    </div>
    <nav class="side">
      <div class="group-label">State</div>
      {"".join(nav_items[:5])}
      <div class="group-label">Activity</div>
      {"".join(nav_items[5:])}
    </nav>
  </aside>
  <main>{body}</main>
</div>
</body>
</html>"""


def _confidence_badge(conf: str) -> str:
    label = html.escape(conf)
    return f'<span class="badge {label}">{label}</span>'


def _entity_link(entity_id: str, *, label: str | None = None) -> str:
    safe_id = html.escape(entity_id)
    text = html.escape(label or (entity_id[:8] + "…"))
    return f'<a href="/entity/{safe_id}" class="entity-ref">{text}</a>'


def _maybe(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else '<span class="dim">—</span>'


def _page_header(title: str, *, subtitle: str | None = None) -> str:
    sub = f'<span class="subtitle">{html.escape(subtitle)}</span>' if subtitle else ""
    return f'<header class="page"><h1>{html.escape(title)}{sub}</h1></header>'


def _empty(title: str, hint: str, code: str | None = None) -> str:
    code_block = f"<code>{html.escape(code)}</code>" if code else ""
    return (
        '<div class="empty">'
        f'<div class="empty-title">{html.escape(title)}</div>'
        f'<div class="empty-hint">{html.escape(hint)}</div>'
        f"{code_block}"
        "</div>"
    )


# ----------------------- routes -----------------------


async def home(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        stats_dict = state.stats(conn)
        recent_sessions = state.recent_sessions(conn, limit=5)
        recent_commitments = state.list_commitments(conn, limit=5)
        recent_blockers = state.list_blockers(conn, limit=3)
    finally:
        conn.close()

    stat_cards = [
        ("Sessions", stats_dict.get("sessions", 0), "sessions"),
        ("Commitments", stats_dict.get("commitments_open", 0), "commitments"),
        ("Decisions", stats_dict.get("decisions_open", 0), "decisions"),
        ("Questions", stats_dict.get("open_questions_open", 0), "questions"),
        ("Blockers", stats_dict.get("blockers_open", 0), "blockers"),
        ("Merged", stats_dict.get("entities_merged", 0), None),
        ("Projections", stats_dict.get("projections_active", 0), None),
    ]
    stats_html = "".join(
        '<div class="stat{muted_cls}">'
        f'<div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-icon">{_ICONS.get(icon, "")}</div>'
        "</div>".format(muted_cls=" muted" if not icon else "")
        for label, value, icon in stat_cards
    )

    if recent_commitments:
        commits_rows = "".join(_render_commitment_row(c) for c in recent_commitments)
        commits_card = f"""
<div class="card">
  <div class="card-header">
    <h3>Recent commitments</h3>
    <span class="count">{len(recent_commitments)}</span>
  </div>
  <table>
    <thead><tr><th>Actor</th><th>Deliverable</th><th>Deadline</th><th>Confidence</th><th>ID</th></tr></thead>
    <tbody>{commits_rows}</tbody>
  </table>
</div>"""
    else:
        commits_card = _empty(
            "No commitments yet",
            "Ingest a transcript to extract structured commitments.",
            "verbatim ingest path/to/meeting.txt",
        )

    blockers_block = ""
    if recent_blockers:
        blockers_rows = "".join(_render_blocker_row(b) for b in recent_blockers)
        blockers_block = f"""
<h2 class="section">Current blockers</h2>
<div class="card">
  <table>
    <thead><tr><th>Blocked thing</th><th>Blocked by</th><th>Owner</th><th>Confidence</th><th>ID</th></tr></thead>
    <tbody>{blockers_rows}</tbody>
  </table>
</div>"""

    sessions_block = ""
    if recent_sessions:
        session_rows = "".join(_render_session_row(s) for s in recent_sessions)
        sessions_block = f"""
<h2 class="section">Recent sessions</h2>
<div class="card">
  <table>
    <thead><tr><th>When (UTC)</th><th>Source</th><th>Kind</th><th>Items</th></tr></thead>
    <tbody>{session_rows}</tbody>
  </table>
</div>"""

    body = (
        _page_header("Verbatim dashboard", subtitle="overview of your team's operational state")
        + f'<div class="stat-grid">{stats_html}</div>'
        + '<h2 class="section">Recent commitments</h2>'
        + commits_card
        + blockers_block
        + sessions_block
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
            _page_header("Commitments")
            + filters_form
            + _empty("No commitments match", "Try removing the filters or broadening your time range.")
        )
    else:
        rows = "".join(_render_commitment_row(c) for c in items)
        body = (
            _page_header("Commitments", subtitle=f"{len(items)} item(s)")
            + filters_form
            + '<div class="card"><table>'
            + "<thead><tr><th>Actor</th><th>Deliverable</th>"
            + "<th>Deadline</th><th>Confidence</th><th>ID</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
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
        body = (
            _page_header("Decisions")
            + filters_form
            + _empty("No decisions match", "Try removing the filters or broadening your time range.")
        )
    else:
        rows = "".join(_render_decision_row(d) for d in items)
        body = (
            _page_header("Decisions", subtitle=f"{len(items)} item(s)")
            + filters_form
            + '<div class="card"><table>'
            + "<thead><tr><th>Topic</th><th>Outcome</th>"
            + "<th>Participants</th><th>Confidence</th><th>ID</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
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
        body = (
            _page_header("Open questions")
            + filters_form
            + _empty("No open questions match", "Try removing the filters.")
        )
    else:
        rows = "".join(_render_question_row(q) for q in items)
        body = (
            _page_header("Open questions", subtitle=f"{len(items)} item(s)")
            + filters_form
            + '<div class="card"><table>'
            + "<thead><tr><th>Topic</th><th>Question</th>"
            + "<th>Raised by</th><th>Confidence</th><th>ID</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
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
        body = (
            _page_header("Blockers")
            + filters_form
            + _empty("No blockers match", "Either there's nothing in the way, or your filters are too narrow.")
        )
    else:
        rows = "".join(_render_blocker_row(b) for b in items)
        body = (
            _page_header("Blockers", subtitle=f"{len(items)} item(s)")
            + filters_form
            + '<div class="card"><table>'
            + "<thead><tr><th>Blocked thing</th><th>Blocked by</th>"
            + "<th>Owner</th><th>Confidence</th><th>ID</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
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
        body = (
            _page_header("Sessions")
            + _empty(
                "No extraction sessions yet",
                "Each ingest creates a session. Run an ingest to see them here.",
                "verbatim ingest path/to/meeting.txt",
            )
        )
    else:
        rows = "".join(_render_session_row(s) for s in items)
        body = (
            _page_header("Sessions", subtitle=f"{len(items)} session(s)")
            + '<div class="card"><table>'
            + "<thead><tr><th>When (UTC)</th><th>Source</th><th>Kind</th>"
            + "<th>Items</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
        )
    return HTMLResponse(_shell("Sessions", body, active="/sessions"))


async def projections(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        items = store.list_projections(conn, status="active", limit=200)
    finally:
        conn.close()
    if not items:
        body = (
            _page_header("Projections")
            + _empty(
                "No active projections",
                "Push commitments to Linear to see them here.",
                "verbatim project linear --team Engineering",
            )
        )
    else:
        rows = "".join(_render_projection_row(p) for p in items)
        body = (
            _page_header("Active projections", subtitle=f"{len(items)} item(s)")
            + '<div class="card"><table>'
            + "<thead><tr><th>Entity</th><th>Target</th><th>Identifier</th>"
            + "<th>URL</th><th>Created</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div>"
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
        body = (
            _page_header("Entity not found")
            + _empty(
                "We couldn't find that entity",
                f"No entity matches the id `{entity_id}`. It may have been deleted "
                "or never existed.",
            )
        )
        return HTMLResponse(_shell("Not found", body), status_code=404)

    payload = entity["payload"]

    # Compose a human-readable headline based on entity kind
    if entity["kind"] == "commitment":
        headline = f"{payload.get('actor') or '?'}: {payload.get('deliverable') or '?'}"
    elif entity["kind"] == "decision":
        headline = f"{payload.get('topic') or '?'} → {payload.get('outcome') or '?'}"
    elif entity["kind"] == "open_question":
        headline = payload.get("question") or payload.get("topic") or "?"
    elif entity["kind"] == "blocker":
        headline = (
            f"{payload.get('blocked_thing') or '?'} "
            f"blocked by {payload.get('blocked_by') or '?'}"
        )
    else:
        headline = entity["kind"]

    # Kind-specific dl rows
    rows: list[tuple[str, str]] = []
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

    # Meta row: kind chip, confidence badge, status, merged info
    meta_parts = [
        f'<span class="pill">{html.escape(entity["kind"])}</span>',
        _confidence_badge(entity["confidence"]),
        f'<span class="pill">status: {html.escape(entity["status"])}</span>',
    ]
    if entity.get("merged_count"):
        meta_parts.append(
            f'<span class="pill">merged from {entity["merged_count"]+1} sources</span>'
        )
    if entity.get("canonical_id"):
        meta_parts.append(
            f'<span class="pill">merged into {_entity_link(entity["canonical_id"])}</span>'
        )
    meta_parts.append(
        f'<span class="pill mono">created {html.escape(entity["created_at"][:19])}</span>'
    )
    meta_html = "".join(meta_parts)

    # Source quote blocks
    quotes_html: list[str] = []
    for s in entity.get("sources", []):
        ts = (
            f'<span class="ts">[{html.escape(s["approximate_timestamp"])}]</span>'
            if s.get("approximate_timestamp") else ""
        )
        speaker = (
            f'<span class="speaker">{html.escape(s["speaker"])}:</span>'
            if s.get("speaker") else ""
        )
        rationale = (
            f'<div class="rationale">{html.escape(s["rationale"])}</div>'
            if s.get("rationale") else ""
        )
        quotes_html.append(
            '<div class="quote">'
            f"{ts}{speaker} {html.escape(s['verbatim_quote'])}"
            f"{rationale}"
            "</div>"
        )
    quotes_block = "".join(quotes_html) or _empty("No sources", "This entity has no source quotes.")

    body = (
        _page_header(headline, subtitle=f'id: {entity_id}')
        + '<div class="entity-detail">'
        + f'<div class="entity-meta">{meta_html}</div>'
        + (f"<dl>{dl_rows}</dl>" if dl_rows else "")
        + f'<h2 class="section">Sources ({len(entity.get("sources", []))})</h2>'
        + quotes_block
        + "</div>"
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
