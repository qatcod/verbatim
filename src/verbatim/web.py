"""Verbatim web UI — three-pane inbox-style app.

Implementation of the Claude Design handoff (the original HTML prototype lives
under `docs/design/verbatim.html`). The home page is now an Inbox: a Linear-style
three-pane shell where the entity list and the detail pane live side by side and
selection drives the URL via a query param.

# Layout

   ┌─────────────┬─────────────────┬────────────────────────┐
   │  sidebar    │   list pane     │   detail pane          │
   │  (232px)    │   (480px)       │   (1fr)                │
   │             │                 │                        │
   │  brand      │   header        │   breadcrumb / actions │
   │  search     │   filter tabs   │   eyebrow              │
   │  nav        │   toolbar       │   entity title         │
   │  …          │   row 1         │   ── QUOTE HERO ──     │
   │  …          │   row 2         │   right rail           │
   │  pulse      │   row …         │                        │
   └─────────────┴─────────────────┴────────────────────────┘

# Routes
  GET  /                       inbox (filter=all, id=current selection)
  GET  /commitments            inbox pre-filtered to commitments
  GET  /decisions              inbox pre-filtered to decisions
  GET  /open-questions         inbox pre-filtered to open questions
  GET  /blockers               inbox pre-filtered to blockers
  GET  /sessions               session list (legacy view)
  GET  /projections            projection list (legacy view)
  GET  /search?q=…             cross-entity search
  GET  /entity/{id}            standalone detail (also linked from rows)
  GET  /dashboard              the pre-v0.8 dashboard (stats + activity feed)

# Theme
Light + dark via `[data-theme="light"]`. Default dark. Toggle in the sidebar
footer; persists in localStorage with a 4-line inline script.
"""
from __future__ import annotations

import hashlib
import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from . import state, store

# ----------------------- DB lifecycle for handlers -----------------------


class _AppState:
    db_path: Path | None = None


def _open_conn() -> sqlite3.Connection:
    return state.open_db(_AppState.db_path)


# ----------------------- icons (inlined SVG, port of bundle.jsx's Ico set) -------


_ICONS: dict[str, str] = {
    # Brand mark — two pairs of pill-shaped marks tilted -12°
    "logo": (
        '<svg width="22" height="22" viewBox="0 0 32 32" fill="currentColor" '
        'aria-label="Verbatim"><g transform="rotate(-12 16 16)">'
        '<rect x="5" y="17" width="3.2" height="9" rx="1.6"/>'
        '<rect x="10" y="17" width="3.2" height="9" rx="1.6"/>'
        '<rect x="18.8" y="6" width="3.2" height="9" rx="1.6"/>'
        '<rect x="23.8" y="6" width="3.2" height="9" rx="1.6"/>'
        "</g></svg>"
    ),
    "inbox": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M2 9h3l1.2 2h3.6L11 9h3M2 9l2-5h8l2 5M2 9v3.5A1.5 1.5 0 0 0 3.5 14h9'
        'a1.5 1.5 0 0 0 1.5-1.5V9"/></svg>'
    ),
    "user": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="8" cy="6" r="2.5"/><path d="M3 13.5c0-2.5 2.2-4 5-4s5 1.5 5 4"/></svg>'
    ),
    "commit": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3 4l3 3-3 3M7 10h6"/></svg>'
    ),
    "decision": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M8 2v12M3 5l5-3 5 3M3 11l5 3 5-3"/></svg>'
    ),
    "question": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="8" cy="8" r="6"/>'
        '<path d="M6.5 6.5c0-1 .7-2 1.6-2s1.6.8 1.6 1.8c0 1.4-1.6 1.6-1.6 2.5M8 11.4v.2"/></svg>'
    ),
    "blocker": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="8" cy="8" r="6"/><path d="M4 4l8 8"/></svg>'
    ),
    "slack": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" '
        'stroke-linecap="round">'
        '<rect x="2" y="6" width="3" height="2" rx="1"/>'
        '<rect x="6" y="2" width="2" height="3" rx="1"/>'
        '<rect x="11" y="8" width="3" height="2" rx="1"/>'
        '<rect x="8" y="11" width="2" height="3" rx="1"/>'
        '<rect x="6" y="6" width="4" height="4" rx="1"/></svg>'
    ),
    "meeting": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="2" y="4" width="9" height="8" rx="1.5"/><path d="M11 7l3-2v6l-3-2z"/></svg>'
    ),
    "github": (
        '<svg viewBox="0 0 16 16" fill="currentColor">'
        '<path d="M8 .2a8 8 0 0 0-2.5 15.6c.4.1.6-.2.6-.4v-1.6c-2.2.5-2.7-1-2.7-1'
        '-.4-.9-.9-1.2-.9-1.2-.7-.5.1-.5.1-.5.8 0 1.2.8 1.2.8.7 1.2 1.9.9 2.4.7'
        '.1-.5.3-.9.5-1.1-1.8-.2-3.6-.9-3.6-3.9 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 '
        '0 0 .7-.2 2.2.8a7.5 7.5 0 0 1 4 0c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.6'
        '.8 1.3.8 2.1 0 3-1.8 3.7-3.6 3.9.3.2.5.7.5 1.4v2c0 .2.2.5.6.4A8 8 0 0 0 8 .2Z"/></svg>'
    ),
    "search": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>'
    ),
    "plus": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round"><path d="M8 3v10M3 8h10"/></svg>'
    ),
    "chevron": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>'
    ),
    "chevronD": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M4 6l4 4 4-4"/></svg>'
    ),
    "filter": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M2 4h12M4 8h8M6 12h4"/></svg>'
    ),
    "sort": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M4 3v10M2 11l2 2 2-2M9 5h5M9 9h4M9 13h2"/></svg>'
    ),
    "arrow": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h10M9 4l4 4-4 4"/></svg>'
    ),
    "link": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M7 9l2-2M6.5 4.5l1-1a2.8 2.8 0 0 1 4 4l-1 1M9.5 11.5l-1 1a2.8 2.8 0 0 1-4-4l1-1"/></svg>'
    ),
    "check": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5 6.5 12 13 4.5"/></svg>'
    ),
    "more": (
        '<svg viewBox="0 0 16 16" fill="currentColor">'
        '<circle cx="4" cy="8" r="1.2"/><circle cx="8" cy="8" r="1.2"/><circle cx="12" cy="8" r="1.2"/></svg>'
    ),
    "sun": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round"><circle cx="8" cy="8" r="3"/>'
        '<path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3"/></svg>'
    ),
    "moon": (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M13 9.5A5.5 5.5 0 1 1 6.5 3a4.5 4.5 0 0 0 6.5 6.5Z"/></svg>'
    ),
    "bolt": (
        '<svg viewBox="0 0 16 16" fill="currentColor">'
        '<path d="M9 1 3 9h4l-1 6 6-8H8z"/></svg>'
    ),
}


# ----------------------- CSS (port of design tokens + components) -----------------------


_CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0a0c;
  --surface: #131316;
  --surface-2: #18181c;
  --surface-3: #1d1d22;
  --hover: #1a1a1f;
  --border: #1f1f24;
  --border-strong: #2a2a31;
  --text: #f4f4f5;
  --text-2: #c4c4c9;
  --text-3: #8a8a93;
  --text-4: #5f5f68;
  --accent: #a78bfa;
  --accent-2: #8b5cf6;
  --accent-soft: rgba(167, 139, 250, 0.12);
  --accent-rail: rgba(167, 139, 250, 0.85);
  --commitment: #a78bfa;
  --decision: #5eead4;
  --question: #fbbf24;
  --blocker: #fb7185;
  --shadow-lg: 0 24px 60px -12px rgba(0,0,0,0.5), 0 2px 6px rgba(0,0,0,0.3);
  --radius: 6px;
  --radius-lg: 10px;
}

[data-theme="light"] {
  color-scheme: light;
  --bg: #ffffff;
  --surface: #fafafa;
  --surface-2: #f5f5f6;
  --surface-3: #ececef;
  --hover: #f3f3f5;
  --border: #e9e9ec;
  --border-strong: #dcdce0;
  --text: #18181b;
  --text-2: #3f3f46;
  --text-3: #71717a;
  --text-4: #a1a1aa;
  --accent: #7c3aed;
  --accent-2: #6d28d9;
  --accent-soft: rgba(124, 58, 237, 0.08);
  --accent-rail: rgba(124, 58, 237, 0.7);
  --commitment: #7c3aed;
  --decision: #0d9488;
  --question: #d97706;
  --blocker: #e11d48;
  --shadow-lg: 0 20px 40px -16px rgba(20,20,30,0.16), 0 1px 3px rgba(20,20,30,0.06);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Inter", system-ui, -apple-system, sans-serif;
  font-feature-settings: "cv11", "ss01", "ss03", "cv02";
  background: var(--bg);
  color: var(--text);
  font-size: 13px;
  line-height: 1.45;
  letter-spacing: -0.005em;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
.app-inbox body, body.app-inbox { overflow: hidden; }
.app-page { padding: 32px 40px 80px; max-width: 1180px; margin: 0 auto; }

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-2); text-decoration: underline; text-underline-offset: 3px; }

/* skip link */
.skip-link {
  position: absolute; top: -40px; left: 8px;
  background: var(--accent); color: #fff;
  padding: 8px 12px; border-radius: 5px;
  font-weight: 600; font-size: 12px; z-index: 100;
  transition: top 0.18s ease;
}
.skip-link:focus { top: 8px; text-decoration: none; }

/* scrollbars */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-4); }

/* focus rings */
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 4px;
}

/* ===== Shell (inbox view) ===== */
.shell {
  display: grid;
  grid-template-columns: 232px 480px 1fr;
  height: 100vh;
  background: var(--bg);
}

/* ===== Sidebar ===== */
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.brand {
  padding: 14px 16px 14px 14px;
  display: flex;
  align-items: center;
  gap: 9px;
  border-bottom: 1px solid var(--border);
  color: var(--accent);
  text-decoration: none;
}
.brand:hover { background: var(--hover); text-decoration: none; }
.brand-word {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.025em;
  color: var(--text);
}
.brand-team {
  font-size: 11px;
  color: var(--text-3);
  font-weight: 500;
  margin-left: 2px;
}
.brand-team::before {
  content: "/";
  margin-right: 6px;
  color: var(--text-4);
  font-weight: 400;
}
.brand-chevron {
  margin-left: auto;
  color: var(--text-4);
  width: 11px; height: 11px;
}

.search-row {
  padding: 10px 10px 8px;
  display: flex;
  gap: 6px;
}
.search-form { flex: 1; display: flex; }
.search-btn {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-3);
  padding: 5px 9px;
  font: inherit;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  outline: none;
}
.search-btn::placeholder { color: var(--text-3); }
.search-btn:hover, .search-btn:focus {
  border-color: var(--border-strong); color: var(--text);
}
.search-btn-wrap {
  flex: 1; position: relative;
}
.search-btn-wrap .ico {
  position: absolute; left: 9px; top: 50%; transform: translateY(-50%);
  width: 12px; height: 12px; color: var(--text-3); pointer-events: none;
}
.search-btn-wrap input {
  width: 100%; padding-left: 28px;
}
.search-btn-wrap .kbd {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  color: var(--text-4); font-size: 10.5px; letter-spacing: 0.02em;
  font-family: ui-monospace, Menlo, monospace;
}

.icon-btn {
  width: 26px; height: 26px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text-3);
  display: grid; place-items: center;
  cursor: pointer;
}
.icon-btn:hover { color: var(--text); border-color: var(--border-strong); }
.icon-btn svg { width: 12px; height: 12px; }

.nav {
  padding: 6px 8px 12px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.nav-section {
  padding: 10px 8px 4px;
  font-size: 10.5px;
  font-weight: 550;
  color: var(--text-4);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 8px;
  border-radius: 5px;
  color: var(--text-2);
  font-size: 13px;
  font-weight: 450;
  line-height: 1.4;
  text-decoration: none;
}
.nav-item:hover { background: var(--hover); color: var(--text); text-decoration: none; }
.nav-item.active { background: var(--hover); color: var(--text); font-weight: 500; }
.nav-item .ico {
  width: 14px; height: 14px;
  color: var(--text-3);
  flex-shrink: 0;
  display: inline-flex;
}
.nav-item.active .ico { color: var(--text); }
.nav-item .ico svg { width: 100%; height: 100%; }
.nav-item .count {
  margin-left: auto;
  color: var(--text-4);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.nav-item.has-dot::after {
  content: "";
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  margin-left: auto;
}
.nav-item.has-dot .count { display: none; }
.nav-item.team .ico {
  width: 6px; height: 6px;
  border-radius: 50%;
  margin-left: 4px;
  margin-right: 4px;
}

.sidebar-footer {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 9px;
}
.ingest-pulse {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #22c55e;
  position: relative;
  flex-shrink: 0;
}
.ingest-pulse::after {
  content: "";
  position: absolute; inset: -3px;
  border-radius: 50%;
  background: #22c55e;
  opacity: 0.3;
  animation: pulse 2.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { transform: scale(0.6); opacity: 0.3; }
  50% { transform: scale(1.2); opacity: 0; }
}
.ingest-label { font-size: 11.5px; color: var(--text-3); }
.ingest-count {
  color: var(--text-2); font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.theme-toggle {
  margin-left: auto;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-3);
  width: 24px; height: 24px;
  border-radius: 5px;
  display: grid; place-items: center;
  cursor: pointer;
}
.theme-toggle:hover { color: var(--text); border-color: var(--border-strong); }
.theme-toggle svg { width: 13px; height: 13px; }
[data-theme="light"] .theme-toggle [data-icon="sun"] { display: none; }
[data-theme="dark"] .theme-toggle [data-icon="moon"] { display: none; }
:root .theme-toggle [data-icon="moon"] { display: none; }
[data-theme="dark"] .theme-toggle [data-icon="sun"] { display: block; }

/* ===== List pane ===== */
.list-pane {
  border-right: 1px solid var(--border);
  background: var(--bg);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.list-header { padding: 14px 20px 0; border-bottom: 1px solid var(--border); }
.list-header-top {
  display: flex; align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.list-title { font-size: 15px; font-weight: 600; letter-spacing: -0.018em; }
.list-meta {
  color: var(--text-3); font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.filter-tabs {
  display: flex; gap: 2px; align-items: center;
  overflow-x: auto;
}
.filter-tab {
  padding: 7px 10px 9px;
  font-size: 12px; font-weight: 500;
  color: var(--text-3);
  border-bottom: 1.5px solid transparent;
  margin-bottom: -1px;
  display: flex; align-items: center; gap: 6px;
  white-space: nowrap;
  text-decoration: none;
}
.filter-tab:hover { color: var(--text); text-decoration: none; }
.filter-tab.active { color: var(--text); border-bottom-color: var(--text); }
.filter-tab .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.filter-tab .ct {
  color: var(--text-4);
  font-variant-numeric: tabular-nums;
  font-weight: 450;
}
.filter-tab.active .ct { color: var(--text-3); }

.toolbar {
  padding: 7px 20px;
  display: flex; gap: 6px; align-items: center;
  border-bottom: 1px solid var(--border);
  color: var(--text-3);
  font-size: 11.5px;
}
.toolbar-chip {
  padding: 3px 7px 4px;
  border: 1px dashed var(--border-strong);
  border-radius: 4px;
  color: var(--text-3);
  display: flex; align-items: center; gap: 4px;
}
.toolbar-chip svg { width: 11px; height: 11px; }
.toolbar-chip.set {
  border-style: solid; border-color: var(--border-strong);
  color: var(--text-2); background: var(--surface);
}
.toolbar-chip.set strong { color: var(--text); font-weight: 500; }
.toolbar-spacer { flex: 1; }
.toolbar-sort {
  display: flex; align-items: center; gap: 4px;
  color: var(--text-3);
}

.list-scroll { overflow-y: auto; flex: 1; min-height: 0; }

/* ===== List row ===== */
.row {
  padding: 12px 20px 14px;
  border-bottom: 1px solid var(--border);
  position: relative;
  display: grid;
  grid-template-columns: auto 1fr auto;
  column-gap: 12px;
  row-gap: 6px;
  align-items: start;
  text-decoration: none;
  color: inherit;
}
.row:hover { background: var(--surface); text-decoration: none; color: inherit; }
.row.selected { background: var(--surface); }
.row.selected::before {
  content: "";
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 2px;
  background: var(--text);
}
.row.unread .row-summary { color: var(--text); font-weight: 500; }
.row.unread::after {
  content: "";
  position: absolute;
  left: 8px; top: 18px;
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--accent);
}

.row-type {
  display: flex; align-items: center; gap: 6px;
  padding-top: 1px;
}
.type-dot {
  width: 8px; height: 8px; border-radius: 2px;
}
.type-dot.commitment { background: var(--commitment); }
.type-dot.decision { background: var(--decision); }
.type-dot.open_question, .type-dot.question { background: var(--question); }
.type-dot.blocker { background: var(--blocker); }

.row-id {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10.5px;
  color: var(--text-4);
  font-weight: 400;
  letter-spacing: -0.01em;
}

.row-summary {
  grid-column: 2;
  font-size: 13px;
  font-weight: 450;
  color: var(--text-2);
  line-height: 1.4;
  letter-spacing: -0.005em;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
}
.row-meta {
  grid-column: 3;
  display: flex; align-items: center; gap: 10px;
  color: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.avatar {
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--surface-3);
  display: grid; place-items: center;
  color: var(--text-2);
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  flex-shrink: 0;
  border: 1px solid var(--border);
}
.avatar.purple { background: #4c2d8a; color: #ddd6fe; border-color: #5b21b6; }
.avatar.teal   { background: #134e4a; color: #99f6e4; border-color: #115e59; }
.avatar.rose   { background: #7f1d1d; color: #fecaca; border-color: #991b1b; }
.avatar.amber  { background: #78350f; color: #fde68a; border-color: #92400e; }
.avatar.blue   { background: #1e3a8a; color: #bfdbfe; border-color: #1e40af; }
.avatar.slate  { background: #334155; color: #e2e8f0; border-color: #475569; }
[data-theme="light"] .avatar.purple { background: #ede9fe; color: #5b21b6; border-color: #ddd6fe; }
[data-theme="light"] .avatar.teal   { background: #ccfbf1; color: #115e59; border-color: #99f6e4; }
[data-theme="light"] .avatar.rose   { background: #ffe4e6; color: #9f1239; border-color: #fecdd3; }
[data-theme="light"] .avatar.amber  { background: #fef3c7; color: #92400e; border-color: #fde68a; }
[data-theme="light"] .avatar.blue   { background: #dbeafe; color: #1e40af; border-color: #bfdbfe; }
[data-theme="light"] .avatar.slate  { background: #e2e8f0; color: #334155; border-color: #cbd5e1; }

.row-quote {
  grid-column: 1 / -1;
  margin-left: 14px;
  padding-left: 10px;
  border-left: 2px solid var(--border-strong);
  color: var(--text-3);
  font-size: 12px;
  font-style: italic;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  letter-spacing: -0.002em;
}
.row.selected .row-quote { border-left-color: var(--accent-rail); color: var(--text-2); }
.row-quote .attr {
  font-style: normal;
  color: var(--text-4);
  font-size: 11px;
  margin-left: 6px;
}
.row-quote .attr::before { content: "— "; }

.row-foot {
  grid-column: 1 / -1;
  margin-left: 14px;
  display: flex; gap: 10px; align-items: center;
  color: var(--text-4);
  font-size: 11px;
}
.src {
  display: inline-flex; align-items: center; gap: 5px;
  color: var(--text-3);
}
.src svg { width: 11px; height: 11px; }
.row-foot .dot {
  width: 2px; height: 2px; border-radius: 50%;
  background: var(--text-4);
}
.row-foot .due { color: var(--text-3); }
.row-foot .due.soon { color: var(--question); }
.row-foot .due.overdue { color: var(--blocker); }
.row-foot .status {
  padding: 1.5px 6px;
  border-radius: 3px;
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: 0.005em;
  border: 1px solid var(--border);
}
.row-foot .status.open { color: var(--text-3); }
.row-foot .status.confirmed {
  color: #5eead4;
  background: rgba(94,234,212,0.08);
  border-color: rgba(94,234,212,0.18);
}
[data-theme="light"] .row-foot .status.confirmed {
  color: #0f766e;
  background: rgba(20,184,166,0.1);
  border-color: rgba(20,184,166,0.22);
}

.list-empty {
  padding: 56px 24px;
  text-align: center;
  color: var(--text-3);
}
.list-empty .empty-title {
  font-size: 14px; font-weight: 600; color: var(--text-2);
  margin-bottom: 6px;
}
.list-empty code {
  display: inline-block; margin-top: 8px;
  background: var(--surface-2); padding: 4px 8px;
  border-radius: 4px; font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11.5px; color: var(--accent);
  border: 1px solid var(--border);
}

/* ===== Detail pane ===== */
.detail {
  background: var(--bg);
  display: flex; flex-direction: column;
  min-height: 0;
  overflow-y: auto;
}
.detail-header {
  padding: 14px 32px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
  position: sticky; top: 0;
  background: var(--bg);
  z-index: 5;
}
.breadcrumb {
  display: flex; align-items: center; gap: 7px;
  color: var(--text-3);
  font-size: 12px;
  flex: 1;
}
.breadcrumb .sep { color: var(--text-4); }
.breadcrumb .id {
  font-family: "JetBrains Mono", monospace;
  font-size: 11.5px; color: var(--text-2);
}
.header-actions { display: flex; gap: 6px; }
.hdr-btn {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-2);
  border-radius: 5px;
  padding: 5px 9px;
  font: inherit;
  font-size: 11.5px;
  font-weight: 500;
  display: flex; align-items: center; gap: 5px;
  text-decoration: none;
}
.hdr-btn:hover {
  background: var(--surface);
  border-color: var(--border-strong);
  color: var(--text);
  text-decoration: none;
}
.hdr-btn svg { width: 12px; height: 12px; }
.hdr-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.hdr-btn.primary:hover {
  background: var(--accent-2); border-color: var(--accent-2);
  color: #fff;
}

.detail-empty {
  padding: 80px 32px;
  text-align: center;
  color: var(--text-3);
}
.detail-empty .empty-mark {
  width: 56px; height: 56px;
  margin: 0 auto 16px;
  color: var(--text-4);
  opacity: 0.6;
}
.detail-empty .empty-title {
  font-size: 16px; font-weight: 600; color: var(--text-2);
  margin-bottom: 6px;
}
.detail-empty .empty-hint { font-size: 13px; }

.detail-body {
  padding: 22px 32px 56px;
  display: grid;
  grid-template-columns: 1fr 240px;
  gap: 32px 40px;
  max-width: 1080px;
}
.detail-main { min-width: 0; }

.entity-eyebrow {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 550;
  margin-bottom: 12px;
}
.entity-eyebrow .type-dot { width: 7px; height: 7px; border-radius: 2px; }
.entity-eyebrow .id {
  font-family: "JetBrains Mono", monospace;
  text-transform: none; letter-spacing: 0;
}
.entity-title {
  font-size: 22px; font-weight: 600;
  letter-spacing: -0.022em;
  line-height: 1.25;
  margin: 0 0 28px;
  color: var(--text);
  text-wrap: balance;
}

/* ===== Quote hero ===== */
.quote-hero {
  position: relative;
  margin: 0 0 28px;
  padding: 18px 20px 18px 28px;
  background: var(--accent-soft);
  border-radius: var(--radius-lg);
  border: 1px solid rgba(167,139,250,0.18);
}
[data-theme="light"] .quote-hero { border-color: rgba(124,58,237,0.18); }
.quote-hero::before {
  content: "";
  position: absolute;
  left: 0; top: 14px; bottom: 14px;
  width: 2.5px;
  border-radius: 2px;
  background: var(--accent);
}
.quote-hero-label {
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 8px;
  display: flex; align-items: center; gap: 6px;
}
.quote-hero-label .lock {
  background: var(--accent);
  color: var(--bg);
  border-radius: 3px;
  padding: 1px 5px;
  font-size: 9.5px;
  letter-spacing: 0.04em;
  font-weight: 600;
}
[data-theme="light"] .quote-hero-label .lock { color: #fff; }
.quote-hero-text {
  font-size: 17px;
  line-height: 1.45;
  color: var(--text);
  font-weight: 450;
  letter-spacing: -0.012em;
  font-style: italic;
  text-wrap: pretty;
}
.quote-hero-text::before { content: "\\201C"; color: var(--accent); font-style: normal; font-weight: 500; }
.quote-hero-text::after { content: "\\201D"; color: var(--accent); font-style: normal; font-weight: 500; }
.quote-hero-attr {
  margin-top: 12px;
  display: flex; align-items: center; gap: 8px;
  font-size: 11.5px;
  color: var(--text-3);
}
.quote-hero-attr .avatar { width: 16px; height: 16px; font-size: 9px; }
.quote-hero-attr strong { color: var(--text-2); font-weight: 550; }
.quote-hero-attr .sep { color: var(--text-4); }

/* Additional quotes block (when multiple sources are merged) */
.more-quotes { margin-bottom: 32px; }
.more-quotes h3 {
  font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.07em; font-weight: 600;
  color: var(--text-3); margin: 0 0 12px;
}
.more-quote {
  padding: 10px 12px 10px 16px;
  margin: 8px 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 2px solid var(--border-strong);
  border-radius: var(--radius);
  font-size: 13px;
  color: var(--text-2);
}
.more-quote .body { font-style: italic; }
.more-quote .body::before { content: "\\201C"; color: var(--text-4); margin-right: 1px; }
.more-quote .body::after { content: "\\201D"; color: var(--text-4); margin-left: 1px; }
.more-quote .attr {
  margin-top: 6px;
  display: flex; align-items: center; gap: 6px;
  color: var(--text-4);
  font-size: 11px;
  font-style: normal;
}
.more-quote .attr strong { color: var(--text-3); font-weight: 500; }

/* ===== Right rail ===== */
.detail-side { padding-top: 4px; }
.side-block { margin-bottom: 26px; }
.side-h {
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--text-4);
  font-weight: 600;
  margin: 0 0 10px;
}
.side-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 5px 0;
  font-size: 12px;
  color: var(--text-2);
  border-bottom: 1px solid var(--border);
}
.side-row:last-child { border-bottom: none; }
.side-row .k { color: var(--text-3); font-size: 11.5px; }
.side-row .v {
  display: flex; align-items: center; gap: 6px;
  color: var(--text); font-weight: 450;
}
.side-row .v.muted { color: var(--text-3); font-weight: 400; }
.confidence-bar {
  height: 4px; width: 56px;
  background: var(--surface-3);
  border-radius: 2px;
  overflow: hidden;
}
.confidence-bar > div { height: 100%; background: var(--accent); border-radius: 2px; }

/* ===== Search results page (already-existing styling, tweaked) ===== */
.search-results-group { margin-bottom: 24px; }
.search-results-group h3 {
  font-size: 12px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-3); margin: 0 0 8px;
  display: flex; align-items: center; gap: 8px;
}
.search-results-group h3 .count {
  background: var(--surface-3); color: var(--text-3);
  padding: 1px 7px; border-radius: 9999px;
  font-size: 11px; font-weight: 500;
}
.result-row {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 12px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 6px;
  text-decoration: none;
  color: inherit;
}
.result-row:hover {
  border-color: var(--border-strong); background: var(--surface-2);
  text-decoration: none; color: inherit;
}
.result-row .result-icon {
  width: 28px; height: 28px; border-radius: 6px;
  background: var(--accent-soft); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.result-row .result-icon svg { width: 14px; height: 14px; }
.result-row .result-body { flex: 1; min-width: 0; }
.result-row .result-title {
  font-weight: 500; color: var(--text); margin-bottom: 2px; font-size: 14px;
}
.result-row .result-sub {
  color: var(--text-3); font-size: 12px;
  display: flex; align-items: center; gap: 8px;
}
.result-row .result-quote {
  margin-top: 6px;
  font-size: 12.5px; color: var(--text-2);
  background: var(--surface-2);
  padding: 6px 10px; border-radius: 4px;
  border-left: 2px solid var(--accent-2);
  font-style: italic;
}
mark { background: var(--accent-soft); color: var(--accent); padding: 0 2px; border-radius: 2px; }

/* ===== Dashboard (legacy) ===== */
.page-title { font-size: 22px; font-weight: 600; margin: 0 0 16px; letter-spacing: -0.022em; }
.page-subtitle { color: var(--text-3); font-size: 13px; margin-left: 12px; }
.section-h-page {
  font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.07em; font-weight: 600;
  color: var(--text-3); margin: 32px 0 12px;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}
.stat {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 18px;
}
.stat .stat-label {
  color: var(--text-3); font-size: 11.5px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.06em;
}
.stat .stat-value {
  font-size: 28px; font-weight: 600; margin-top: 6px;
  letter-spacing: -0.02em; color: var(--text);
}
.activity-feed {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 6px 0;
}
.activity-item {
  display: flex; gap: 12px; padding: 10px 18px;
  border-bottom: 1px solid var(--border);
  align-items: flex-start;
}
.activity-item:last-child { border-bottom: none; }
.activity-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); flex-shrink: 0; margin-top: 7px;
}
.activity-body { flex: 1; }
.activity-when {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  color: var(--text-3); font-size: 12px;
}
.activity-text { color: var(--text-2); font-size: 13.5px; }
.mono { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12.5px; color: var(--text-3); }

/* ===== Responsive — collapse below 1080px ===== */
@media (max-width: 1080px) {
  .shell { grid-template-columns: 200px 1fr; }
  .detail { display: none; }
}
@media (max-width: 720px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar { display: none; }
}
"""


# ----------------------- theme + keyboard scripts ----------------------------------


_HEAD_SCRIPT = """
<script>
(function () {
  // Apply persisted theme before paint to avoid flash
  var t = localStorage.getItem('verbatim-theme');
  if (t === 'light' || t === 'dark') {
    document.documentElement.setAttribute('data-theme', t);
  } else {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
})();
</script>
"""

_FOOT_SCRIPT = """
<script>
(function () {
  // `/` focuses search
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && !['INPUT','TEXTAREA'].includes(e.target.tagName)) {
      var box = document.getElementById('sidebar-search-input');
      if (box) { e.preventDefault(); box.focus(); box.select(); }
    }
  });
  // theme toggle
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    var nxt = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nxt);
    try { localStorage.setItem('verbatim-theme', nxt); } catch (err) {}
  });
})();
</script>
"""


# ----------------------- helpers -----------------------


_AVATAR_COLORS = ["purple", "teal", "rose", "amber", "blue", "slate"]


def _short_id(entity_id: str) -> str:
    return f"VRB-{entity_id[:8]}"


def _confidence_pct(confidence: str) -> int:
    return {"high": 95, "medium": 75, "low": 55}.get(confidence, 50)


def _avatar_color(name: str | None) -> str:
    if not name:
        return "slate"
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16)
    return _AVATAR_COLORS[h % len(_AVATAR_COLORS)]


def _initials(name: str | None) -> str:
    if not name:
        return "?"
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return parts[0][:2].upper()


def _avatar(name: str | None, *, size: int = 18) -> str:
    color = _avatar_color(name)
    initials = _initials(name)
    style = ""
    if size != 18:
        font_size = max(8, int(size * 0.42))
        style = f' style="width:{size}px;height:{size}px;font-size:{font_size}px"'
    return (
        f'<span class="avatar {color}" title="{html.escape(name or "")}"{style}>'
        f"{html.escape(initials)}</span>"
    )


def _design_source_kind(internal: str | None) -> str:
    if not internal:
        return "meeting"
    if "slack" in internal:
        return "slack"
    if "github" in internal:
        return "pr"
    return "meeting"


_SOURCE_KIND_LABELS = {"slack": "Slack", "meeting": "Meeting", "pr": "Pull request"}


def _channel_from_source(source_path: str | None) -> str:
    if not source_path:
        return "(none)"
    if source_path.startswith("slack://"):
        try:
            return source_path.split("#", 1)[1].split("/", 1)[0]
        except IndexError:
            return source_path
    if source_path.startswith("github://"):
        try:
            parts = source_path.replace("github://", "").split("/")
            return f"{parts[0]}/{parts[1]}#{parts[3]}"
        except IndexError:
            return source_path
    return source_path.split("/")[-1]


def _ts_relative(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso[:19]
    delta = datetime.now(timezone.utc) - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    if days < 7:
        return f"{days}d ago"
    return dt.strftime("%b %d")


def _is_unread(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < 86400


def _entity_summary(entity: dict[str, Any]) -> str:
    p = entity["payload"]
    k = entity["kind"]
    if k == "commitment":
        actor = p.get("actor")
        deliverable = p.get("deliverable") or ""
        return f"{actor} to {deliverable}" if actor else deliverable
    if k == "decision":
        topic = p.get("topic") or "?"
        outcome = p.get("outcome") or "?"
        return f"{topic} → {outcome}"
    if k == "open_question":
        return p.get("question") or p.get("topic") or "?"
    if k == "blocker":
        return f"{p.get('blocked_thing') or '?'} blocked on {p.get('blocked_by') or '?'}"
    return entity["id"][:16]


def _primary_actor(entity: dict[str, Any]) -> str | None:
    """Best-effort 'who is responsible' for the entity, by kind."""
    p = entity["payload"]
    k = entity["kind"]
    if k == "commitment":
        return p.get("actor")
    if k == "decision":
        parts = p.get("participants") or []
        return parts[0] if parts else None
    if k == "open_question":
        return p.get("raised_by")
    if k == "blocker":
        return p.get("owner")
    return None


# ----------------------- shell (HTML page wrapper) -----------------------


def _shell(
    title: str,
    body: str,
    *,
    body_class: str = "",
    search_q: str = "",
    extra_head: str = "",
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Verbatim</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;550;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>{_CSS}</style>
{_HEAD_SCRIPT}
{extra_head}
</head>
<body class="{body_class}">
<a href="#main-content" class="skip-link">Skip to content</a>
{body}
{_FOOT_SCRIPT}
</body>
</html>"""


# ----------------------- sidebar -----------------------


def _sidebar_html(*, active_filter: str, counts: dict[str, int], search_q: str = "") -> str:
    """Render the left sidebar with brand, search, nav, footer."""
    workspace_items = [
        ("commit", "Commitments", "/commitments", counts.get("commitment", 0)),
        ("decision", "Decisions", "/decisions", counts.get("decision", 0)),
        ("question", "Questions", "/open-questions", counts.get("open_question", 0)),
        ("blocker", "Blockers", "/blockers", counts.get("blocker", 0)),
    ]

    def render_nav_item(
        icon_key: str, label: str, href: str, count: int,
        *, active: bool = False, has_dot: bool = False, dot_color: str = "",
    ) -> str:
        cls = "nav-item"
        if active:
            cls += " active"
        if has_dot:
            cls += " has-dot"
        aria = ' aria-current="page"' if active else ""
        ico = (
            f'<span class="ico" style="background:{dot_color}"></span>'
            if dot_color else f'<span class="ico">{_ICONS.get(icon_key, "")}</span>'
        )
        count_html = (
            f'<span class="count">{count}</span>' if count is not None and not has_dot else ""
        )
        return (
            f'<a href="{html.escape(href)}" class="{cls}"{aria}>'
            f"{ico}<span>{html.escape(label)}</span>{count_html}</a>"
        )

    # Inbox + My items
    inbox_html = render_nav_item(
        "inbox", "Inbox", "/", counts.get("unread", 0),
        active=(active_filter == "all"), has_dot=bool(counts.get("unread")),
    )
    if not counts.get("unread"):
        # When no unread, show count (not dot)
        inbox_html = render_nav_item(
            "inbox", "Inbox", "/", counts.get("total", 0),
            active=(active_filter == "all"),
        )

    workspace_html = "".join(
        render_nav_item(
            icon, label, href, count,
            active=(active_filter == href.lstrip("/").replace("-", "_")
                    or active_filter == icon
                    or (active_filter == "open_question" and href == "/open-questions")
                    or (active_filter == "commitment" and href == "/commitments")
                    or (active_filter == "decision" and href == "/decisions")
                    or (active_filter == "blocker" and href == "/blockers")),
        )
        for icon, label, href, count in workspace_items
    )

    activity_html = (
        '<a href="/people" class="nav-item">'
        f'<span class="ico">{_ICONS.get("inbox", "")}</span><span>People</span>'
        f'<span class="count">{counts.get("people", 0)}</span></a>'
        '<a href="/sessions" class="nav-item">'
        f'<span class="ico">{_ICONS["meeting"]}</span><span>Sessions</span>'
        f'<span class="count">{counts.get("sessions", 0)}</span></a>'
        '<a href="/projections" class="nav-item">'
        f'<span class="ico">{_ICONS["bolt"]}</span><span>Projections</span>'
        f'<span class="count">{counts.get("projections", 0)}</span></a>'
        '<a href="/dashboard" class="nav-item">'
        f'<span class="ico">{_ICONS["arrow"]}</span><span>Dashboard</span></a>'
    )

    search_form = (
        '<form class="search-form" method="get" action="/search" role="search">'
        '<div class="search-btn-wrap">'
        f'<span class="ico">{_ICONS["search"]}</span>'
        f'<input class="search-btn" type="text" name="q" id="sidebar-search-input" '
        f'placeholder="Search or jump to…" value="{html.escape(search_q)}" '
        'autocomplete="off" aria-label="Search Verbatim state">'
        '<span class="kbd">/</span>'
        "</div></form>"
    )

    # Total ingested today — show session count as a proxy
    ingest_count = counts.get("sessions", 0)

    return (
        '<aside class="sidebar">'
        '<a class="brand" href="/" aria-label="Verbatim home">'
        f'{_ICONS["logo"]}'
        '<span class="brand-word">verbatim</span>'
        '<span class="brand-team">engineering</span>'
        f'<span class="brand-chevron">{_ICONS["chevronD"]}</span>'
        "</a>"
        '<div class="search-row">'
        f"{search_form}"
        f'<button class="icon-btn" title="New" aria-label="New">{_ICONS["plus"]}</button>'
        "</div>"
        '<nav class="nav" aria-label="Primary">'
        + inbox_html
        + '<div class="nav-section">Workspace</div>'
        + workspace_html
        + '<div class="nav-section">Activity</div>'
        + activity_html
        + "</nav>"
        '<div class="sidebar-footer">'
        '<div class="ingest-pulse" aria-hidden="true"></div>'
        f'<span class="ingest-label">Sessions · <span class="ingest-count">{ingest_count}</span></span>'
        '<button class="theme-toggle" id="theme-toggle" aria-label="Toggle theme">'
        f'<span data-icon="moon">{_ICONS["moon"]}</span>'
        f'<span data-icon="sun">{_ICONS["sun"]}</span>'
        "</button>"
        "</div>"
        "</aside>"
    )


# ----------------------- list pane (rows + filter tabs) -----------------------


def _row_html(entity: dict[str, Any], *, selected: bool = False) -> str:
    kind = entity["kind"]
    short = _short_id(entity["id"])
    actor = _primary_actor(entity)
    summary = _entity_summary(entity)
    confidence = entity["confidence"]

    payload = entity["payload"]
    deadline = payload.get("deadline") if kind == "commitment" else None

    # First source quote for the inline italic. dict.get(key, default) returns
    # None when the key exists but has None value — protect with `or ""`.
    sources = entity.get("sources") or []
    quote = (sources[0].get("verbatim_quote") or "") if sources else ""
    speaker = (sources[0].get("speaker") or (actor or "")) if sources else (actor or "")

    cls = "row"
    if selected:
        cls += " selected"
    if _is_unread(entity.get("created_at")):
        cls += " unread"

    href = f"/?id={html.escape(entity['id'])}"
    avatar_html = _avatar(actor) if actor else _avatar("?")

    due_html = (
        f'<span class="due">{html.escape(deadline)}</span>' if deadline else
        '<span style="color:var(--text-4)">—</span>'
    )

    confirmed = confidence == "high"
    status_html = (
        '<span class="status confirmed">✓ confirmed</span>' if confirmed
        else '<span class="status open">unconfirmed</span>'
    )

    src_kind = _design_source_kind(entity.get("source_kind") or entity.get("session", {}).get("source_kind"))
    src_label = _SOURCE_KIND_LABELS.get(src_kind, "Source")
    src_icon = _ICONS.get({"slack": "slack", "meeting": "meeting", "pr": "github"}[src_kind], "")
    channel = _channel_from_source(entity.get("source_path"))
    ts = _ts_relative(entity.get("created_at"))

    quote_html = (
        f'<div class="row-quote"><q>{html.escape(quote)}</q>'
        f'<span class="attr">{html.escape((speaker or "").split(" ")[0])} · {html.escape(channel)}</span>'
        "</div>"
    ) if quote else ""

    return (
        f'<a class="{cls}" href="{href}">'
        '<div class="row-type">'
        f'<span class="type-dot {kind}"></span>'
        f'<span class="row-id">{html.escape(short)}</span>'
        "</div>"
        f'<div class="row-summary">{html.escape(summary)}</div>'
        '<div class="row-meta">'
        f"{avatar_html}"
        f"{due_html}"
        "</div>"
        f"{quote_html}"
        '<div class="row-foot">'
        f'<span class="src">{src_icon}<span>{html.escape(src_label)}</span></span>'
        '<span class="dot"></span>'
        f"<span>{html.escape(channel)}</span>"
        '<span class="dot"></span>'
        f"<span>{html.escape(ts)}</span>"
        '<span class="dot"></span>'
        f"{status_html}"
        "</div>"
        "</a>"
    )


def _filter_tab(
    label: str, count: int, *, href: str, active: bool = False, dot: str = "",
) -> str:
    dot_html = f'<span class="dot type-dot {dot}"></span>' if dot else ""
    return (
        f'<a class="filter-tab {"active" if active else ""}" href="{html.escape(href)}">'
        f"{dot_html}<span>{html.escape(label)}</span>"
        f'<span class="ct">{count}</span></a>'
    )


def _list_pane_html(
    *, items: list[dict[str, Any]], selected_id: str | None,
    title: str, current_filter: str, counts: dict[str, int],
) -> str:
    filter_tabs = (
        _filter_tab("All", counts.get("all", 0), href="/", active=(current_filter == "all"))
        + _filter_tab("Commitments", counts.get("commitment", 0),
                      href="/commitments", active=(current_filter == "commitment"),
                      dot="commitment")
        + _filter_tab("Decisions", counts.get("decision", 0),
                      href="/decisions", active=(current_filter == "decision"),
                      dot="decision")
        + _filter_tab("Questions", counts.get("open_question", 0),
                      href="/open-questions", active=(current_filter == "open_question"),
                      dot="question")
        + _filter_tab("Blockers", counts.get("blocker", 0),
                      href="/blockers", active=(current_filter == "blocker"),
                      dot="blocker")
    )

    rows_html = (
        "".join(_row_html(it, selected=(it["id"] == selected_id)) for it in items)
        if items
        else (
            '<div class="list-empty">'
            '<div class="empty-title">No items</div>'
            "<div>Ingest a transcript to populate this view.</div>"
            "<code>verbatim ingest path/to/meeting.txt</code>"
            "</div>"
        )
    )

    return (
        '<section class="list-pane" aria-label="Entities">'
        '<div class="list-header">'
        '<div class="list-header-top">'
        f'<div class="list-title">{html.escape(title)}</div>'
        f'<div class="list-meta">{len(items)} item{"s" if len(items) != 1 else ""}</div>'
        "</div>"
        f'<div class="filter-tabs">{filter_tabs}</div>'
        "</div>"
        '<div class="toolbar">'
        '<div class="toolbar-chip set">'
        f'{_ICONS["filter"]}'
        '<span>Status</span><strong>open</strong>'
        "</div>"
        '<div class="toolbar-spacer"></div>'
        '<div class="toolbar-sort">'
        f'{_ICONS["sort"]}<span>Newest</span>{_ICONS["chevronD"]}'
        "</div>"
        "</div>"
        f'<div class="list-scroll">{rows_html}</div>'
        "</section>"
    )


# ----------------------- detail pane -----------------------


_KIND_LABELS = {
    "commitment": "Commitment",
    "decision": "Decision",
    "open_question": "Question",
    "blocker": "Blocker",
}
_KIND_PLURAL = {
    "commitment": "Commitments",
    "decision": "Decisions",
    "open_question": "Questions",
    "blocker": "Blockers",
}


def _detail_pane_html(entity: dict[str, Any] | None) -> str:
    if entity is None:
        return (
            '<section class="detail" id="main-content">'
            '<div class="detail-empty">'
            f'<div class="empty-mark">{_ICONS["logo"]}</div>'
            '<div class="empty-title">Select an item</div>'
            '<div class="empty-hint">Pick an entity from the list to see its quote evidence and properties.</div>'
            "</div></section>"
        )

    kind = entity["kind"]
    short = _short_id(entity["id"])
    kind_label = _KIND_LABELS.get(kind, kind.title())
    plural = _KIND_PLURAL.get(kind, kind.title())
    actor = _primary_actor(entity)
    summary = _entity_summary(entity)

    sources = entity.get("sources") or []
    primary_source = sources[0] if sources else None
    quote = ((primary_source or {}).get("verbatim_quote") or "") if primary_source else ""
    speaker = (primary_source or {}).get("speaker") or actor or ""

    channel = _channel_from_source(entity.get("source_path"))
    src_kind = _design_source_kind(entity.get("source_kind") or entity.get("session", {}).get("source_kind"))
    src_kind_icon = _ICONS.get({"slack": "slack", "meeting": "meeting", "pr": "github"}[src_kind], "")
    ts = _ts_relative(entity.get("created_at"))

    # Render the hero quote
    quote_hero = (
        '<div class="quote-hero">'
        '<div class="quote-hero-label">'
        '<span class="lock">verbatim</span>'
        "<span>exact words from source · evidence locked</span>"
        "</div>"
        f'<div class="quote-hero-text">{html.escape(quote)}</div>'
        '<div class="quote-hero-attr">'
        f"{_avatar(speaker, size=16)}"
        f"<strong>{html.escape(speaker or 'unknown')}</strong>"
        '<span class="sep">·</span>'
        f"<span>{html.escape(channel)}</span>"
        '<span class="sep">·</span>'
        f"<span>{html.escape(ts)}</span>"
        "</div></div>"
    ) if quote else ""

    # Render additional sources (merged siblings)
    more_quotes_html = ""
    if len(sources) > 1:
        items_html = "".join(
            '<div class="more-quote">'
            f'<div class="body">{html.escape(s.get("verbatim_quote") or "")}</div>'
            '<div class="attr">'
            f"{_avatar(s.get('speaker'), size=14)}"
            f"<strong>{html.escape(s.get('speaker') or 'unknown')}</strong>"
            f"<span>{html.escape(s.get('approximate_timestamp') or '')}</span>"
            "</div></div>"
            for s in sources[1:]
        )
        more_quotes_html = (
            '<div class="more-quotes">'
            f"<h3>Additional sources ({len(sources) - 1})</h3>"
            f"{items_html}"
            "</div>"
        )

    # Properties side block
    payload = entity["payload"]
    prop_rows: list[tuple[str, str]] = [
        ("Type", f'<span class="type-dot {kind}" style="width:7px;height:7px"></span> {html.escape(kind_label)}'),
    ]
    if actor:
        prop_rows.append(("Owner", f"{_avatar(actor, size=16)} {html.escape(actor)}"))
    prop_rows.append(("Status", html.escape(entity["status"])))
    if payload.get("deadline"):
        prop_rows.append(("Due", html.escape(payload["deadline"])))
    if entity.get("merged_count"):
        n = entity["merged_count"]
        word = "source" if n == 1 else "sources"
        prop_rows.append(("Sources", f"merged with {n} other {word}"))
    if entity.get("canonical_id"):
        prop_rows.append((
            "Canonical",
            f'<a href="/?id={html.escape(entity["canonical_id"])}" class="mono">{html.escape(entity["canonical_id"][:8])}</a>',
        ))
    prop_rows.append(("Extracted", html.escape(ts)))

    properties_html = "".join(
        f'<div class="side-row"><span class="k">{html.escape(k)}</span>'
        f'<span class="v">{v}</span></div>'
        for k, v in prop_rows
    )

    # Evidence side block
    confidence = entity["confidence"]
    pct = _confidence_pct(confidence)
    evidence_html = (
        '<div class="side-row">'
        '<span class="k">Confidence</span>'
        '<span class="v" style="gap:8px">'
        f'<span class="confidence-bar"><div style="width:{pct}%"></div></span>'
        f'<span style="font-variant-numeric:tabular-nums">{pct}% · {html.escape(confidence)}</span>'
        "</span></div>"
        '<div class="side-row">'
        '<span class="k">Source</span>'
        f'<span class="v">{src_kind_icon} {html.escape(channel)}</span>'
        "</div>"
    )

    audit_html = _render_audit_trail(entity.get("audit") or [])

    return f"""
<section class="detail" id="main-content">
  <header class="detail-header">
    <div class="breadcrumb">
      <span>Inbox</span>
      <span class="sep">/</span>
      <span>{html.escape(plural)}</span>
      <span class="sep">/</span>
      <span class="id">{html.escape(short)}</span>
    </div>
    <div class="header-actions">
      <a class="hdr-btn" href="/entity/{html.escape(entity["id"])}">
        {_ICONS["link"]}<span>Permalink</span>
      </a>
    </div>
  </header>
  <div class="detail-body">
    <div class="detail-main">
      <div class="entity-eyebrow">
        <span class="type-dot {kind}"></span>
        <span>{html.escape(kind_label)}</span>
        <span style="color:var(--text-4)">·</span>
        <span class="id">{html.escape(short)}</span>
      </div>
      <h1 class="entity-title">{html.escape(summary)}</h1>
      {quote_hero}
      {more_quotes_html}
      {audit_html}
    </div>
    <aside class="detail-side">
      <div class="side-block">
        <h4 class="side-h">Properties</h4>
        {properties_html}
      </div>
      <div class="side-block">
        <h4 class="side-h">Evidence</h4>
        {evidence_html}
      </div>
    </aside>
  </div>
</section>
"""


_AUDIT_ACTION_LABEL = {
    "confirm": "Confirmed",
    "dismiss": "Dismissed",
    "edit": "Edited",
    "reassign": "Reassigned",
    "resolve": "Resolved",
    "create": "Created",
    "merge": "Merged",
    "unlink": "Unlinked",
}


def _render_audit_trail(audit: list[dict[str, Any]]) -> str:
    """Render the audit log as a compact timeline under the entity body."""
    if not audit:
        return ""
    items: list[str] = []
    for row in audit:
        action_label = _AUDIT_ACTION_LABEL.get(row["action"], row["action"].title())
        when = _ts_relative(row.get("created_at"))
        who_raw = row.get("actor_label") or row.get("actor_id") or "—"
        # actor_label may already contain Slack <@USER> mrkdwn from a Slack
        # bot action — render the raw text rather than rendering as a link.
        who = html.escape(who_raw)
        note = row.get("note")
        note_html = (
            f'<div class="audit-note">{html.escape(note)}</div>' if note else ""
        )
        items.append(
            '<li class="audit-item">'
            f'<div class="audit-head">'
            f'<strong>{html.escape(action_label)}</strong>'
            f'<span class="audit-when">{html.escape(when)}</span>'
            "</div>"
            f'<div class="audit-actor">by {who}</div>'
            f"{note_html}"
            "</li>"
        )
    return (
        '<div class="audit-trail">'
        '<h3 class="audit-h">Activity</h3>'
        '<ol class="audit-list">'
        + "".join(items)
        + "</ol></div>"
    )


# ----------------------- counts -----------------------


def _all_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Compute counts used in the sidebar + tabs."""
    out: dict[str, int] = {}
    for kind in ("commitment", "decision", "open_question", "blocker"):
        out[kind] = store.fetch_entities(
            conn, kind=kind, status="open", canonical_only=True, limit=10000
        )
        out[kind] = len(out[kind])
    out["all"] = sum(out[k] for k in ("commitment", "decision", "open_question", "blocker"))
    out["total"] = out["all"]
    s = state.stats(conn)
    out["sessions"] = s.get("sessions", 0)
    out["projections"] = s.get("projections_active", 0)
    out["people"] = len(store.list_known_people(conn, limit=10000))
    return out


def _list_for_filter(
    conn: sqlite3.Connection, filter_kind: str, *, limit: int = 200,
) -> list[dict[str, Any]]:
    """Return canonical entities for a given filter (or all kinds when 'all')."""
    if filter_kind == "all":
        items: list[dict[str, Any]] = []
        for k in ("commitment", "decision", "open_question", "blocker"):
            items.extend(
                store.fetch_entities(
                    conn, kind=k, status="open", canonical_only=True, limit=limit
                )
            )
        items.sort(key=lambda e: e["created_at"], reverse=True)
        return items[:limit]
    return store.fetch_entities(
        conn, kind=filter_kind, status="open", canonical_only=True, limit=limit
    )


def _enrich_entity_with_session(
    conn: sqlite3.Connection, entity: dict[str, Any]
) -> dict[str, Any]:
    """Attach session source_path and source_kind to the entity dict for rendering."""
    row = conn.execute(
        "SELECT source_path, source_kind FROM sessions WHERE id = ?",
        (entity["session_id"],),
    ).fetchone()
    if row:
        entity["source_path"] = row["source_path"]
        entity["source_kind"] = row["source_kind"]
    return entity


# ----------------------- inbox route (handles / and the kind-filtered routes) ---


async def inbox(request: Request) -> HTMLResponse:
    return await _render_inbox(request, filter_kind="all", title="Inbox")


async def commitments(request: Request) -> HTMLResponse:
    return await _render_inbox(request, filter_kind="commitment", title="Commitments")


async def decisions(request: Request) -> HTMLResponse:
    return await _render_inbox(request, filter_kind="decision", title="Decisions")


async def open_questions(request: Request) -> HTMLResponse:
    return await _render_inbox(request, filter_kind="open_question", title="Open questions")


async def blockers(request: Request) -> HTMLResponse:
    return await _render_inbox(request, filter_kind="blocker", title="Blockers")


async def _render_inbox(request: Request, *, filter_kind: str, title: str) -> HTMLResponse:
    selected_id = request.query_params.get("id")

    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        items = _list_for_filter(conn, filter_kind, limit=200)
        for e in items:
            _enrich_entity_with_session(conn, e)

        # Default selection: first item, when no id is given
        if not selected_id and items:
            selected_id = items[0]["id"]

        selected_entity = None
        if selected_id:
            selected_entity = state.show_entity(conn, selected_id)
            if selected_entity:
                _enrich_entity_with_session(conn, selected_entity)
    finally:
        conn.close()

    sidebar = _sidebar_html(active_filter=filter_kind, counts=counts)
    list_pane = _list_pane_html(
        items=items, selected_id=selected_id,
        title=title, current_filter=filter_kind, counts={**counts, "all": counts.get("total", 0)},
    )
    detail_pane = _detail_pane_html(selected_entity)

    body = f'<div class="shell">{sidebar}{list_pane}{detail_pane}</div>'
    return HTMLResponse(_shell(title, body, body_class="app-inbox"))


# ----------------------- entity detail (standalone) ----------------------------


async def entity_detail(request: Request) -> HTMLResponse:
    entity_id = request.path_params["entity_id"]
    conn = _open_conn()
    try:
        entity = state.show_entity(conn, entity_id)
        if entity:
            _enrich_entity_with_session(conn, entity)
            entity["audit"] = store.fetch_audit(conn, entity["id"])
        counts = _all_counts(conn) if entity else {}
    finally:
        conn.close()
    if entity is None:
        body = _shell_with_sidebar(
            inner=(
                '<main id="main-content" class="app-page">'
                '<h1 class="page-title">Entity not found</h1>'
                f'<p>No entity matches the id <code>{html.escape(entity_id)}</code>.</p>'
                "</main>"
            ),
            counts={},
            active="",
        )
        return HTMLResponse(_shell("Not found", body, body_class=""), status_code=404)

    sidebar = _sidebar_html(active_filter=entity["kind"], counts=counts)
    detail = _detail_pane_html(entity)
    body = (
        '<div class="shell" style="grid-template-columns:232px 1fr">'
        f"{sidebar}"
        f"{detail}"
        "</div>"
    )
    return HTMLResponse(_shell("Entity", body, body_class="app-inbox"))


# ----------------------- search route ------------------------------------------


_SEARCH_KIND_LABELS = {
    "commitment": "Commitments",
    "decision": "Decisions",
    "open_question": "Open questions",
    "blocker": "Blockers",
    "source_match": "Source quote matches",
}


def _highlight(text: str, query: str) -> str:
    if not text or not query:
        return html.escape(text or "")
    out: list[str] = []
    lower_text = text.lower()
    lower_query = query.lower()
    i = 0
    qlen = len(query)
    while True:
        idx = lower_text.find(lower_query, i)
        if idx == -1:
            out.append(html.escape(text[i:]))
            break
        out.append(html.escape(text[i:idx]))
        out.append("<mark>" + html.escape(text[idx : idx + qlen]) + "</mark>")
        i = idx + qlen
    return "".join(out)


def _result_summary(entity: dict[str, Any]) -> str:
    return _entity_summary(entity)


def _render_search_result(entity: dict[str, Any], query: str) -> str:
    title = _highlight(_result_summary(entity), query)
    kind_label = entity["kind"].replace("_", " ")
    short = _short_id(entity["id"])
    sub = (
        f'<span class="badge {html.escape(entity["confidence"])}">{html.escape(entity["confidence"])}</span>'
        f' · <span class="mono">{html.escape(short)}</span>'
        f' · {html.escape(kind_label)}'
    )

    matching_quote = ""
    for s in entity.get("sources", []):
        q = s.get("verbatim_quote") or ""
        if query.lower() in q.lower():
            matching_quote = f'<div class="result-quote">{_highlight(q, query)}</div>'
            break
    if not matching_quote and entity.get("sources"):
        q = entity["sources"][0].get("verbatim_quote") or ""
        if q:
            matching_quote = f'<div class="result-quote">{_highlight(q, query)}</div>'

    return (
        f'<a href="/?id={html.escape(entity["id"])}" class="result-row">'
        f'<span class="result-icon">{_ICONS["search"]}</span>'
        '<div class="result-body">'
        f'<div class="result-title">{title}</div>'
        f'<div class="result-sub">{sub}</div>'
        f"{matching_quote}"
        "</div></a>"
    )


async def search_page(request: Request) -> HTMLResponse:
    q = (request.query_params.get("q") or "").strip()

    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        results = state.search(conn, q) if q else None
    finally:
        conn.close()

    sidebar = _sidebar_html(active_filter="", counts=counts, search_q=q)

    if not q:
        page_inner = (
            '<main id="main-content" class="app-page">'
            '<h1 class="page-title">Search</h1>'
            '<p style="color:var(--text-3)">Type something. Match is case-insensitive across actor, '
            "topic, payload, and verbatim source quotes.</p>"
            "</main>"
        )
    else:
        total = sum(len(v) for v in (results or {}).values())
        if total == 0:
            page_inner = (
                '<main id="main-content" class="app-page">'
                f'<h1 class="page-title">Search: "{html.escape(q)}"</h1>'
                '<p style="color:var(--text-3)">No matches. Try fewer characters or a different word.</p>'
                "</main>"
            )
        else:
            sections: list[str] = []
            for kind in ("commitment", "decision", "open_question", "blocker", "source_match"):
                items = (results or {}).get(kind, [])
                if not items:
                    continue
                label = _SEARCH_KIND_LABELS[kind]
                rows_html = "".join(_render_search_result(e, q) for e in items)
                sections.append(
                    '<div class="search-results-group">'
                    f'<h3>{html.escape(label)} <span class="count">{len(items)}</span></h3>'
                    f"{rows_html}"
                    "</div>"
                )
            page_inner = (
                '<main id="main-content" class="app-page">'
                f'<h1 class="page-title">Search: "{html.escape(q)}"<span class="page-subtitle">{total} match(es)</span></h1>'
                + "".join(sections)
                + "</main>"
            )

    body = (
        '<div class="shell" style="grid-template-columns:232px 1fr">'
        + sidebar + page_inner + "</div>"
    )
    return HTMLResponse(_shell("Search", body, body_class="app-inbox", search_q=q))


# ----------------------- sessions, projections, dashboard (legacy two-column) ---


def _shell_with_sidebar(*, inner: str, counts: dict[str, int], active: str = "") -> str:
    sidebar = _sidebar_html(active_filter=active, counts=counts)
    return (
        '<div class="shell" style="grid-template-columns:232px 1fr">'
        + sidebar + inner + "</div>"
    )


async def sessions(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        items = state.recent_sessions(conn, limit=100)
    finally:
        conn.close()

    if not items:
        rows_html = (
            '<div class="list-empty">'
            '<div class="empty-title">No sessions yet</div>'
            "<div>Run an ingest to see them here.</div>"
            "<code>verbatim ingest path/to/meeting.txt</code>"
            "</div>"
        )
    else:
        items_html = "".join(
            '<div class="activity-item">'
            '<span class="activity-dot"></span>'
            '<div class="activity-body">'
            f'<div class="activity-text">'
            f'Ingested <strong>{s.get("entity_count", 0)}</strong> items from '
            f'<span class="mono">{html.escape(s.get("source_path") or "<stdin>")}</span> '
            f'<span style="color:var(--text-3)">({html.escape(s.get("source_kind") or "—")})</span>'
            "</div>"
            f'<div class="activity-when">{html.escape(s["extracted_at"][:19].replace("T", " "))} UTC</div>'
            "</div></div>"
            for s in items
        )
        rows_html = f'<div class="activity-feed">{items_html}</div>'

    inner = (
        '<main id="main-content" class="app-page">'
        f'<h1 class="page-title">Sessions<span class="page-subtitle">{len(items)} ingest{"s" if len(items) != 1 else ""}</span></h1>'
        + rows_html
        + "</main>"
    )
    return HTMLResponse(
        _shell("Sessions", _shell_with_sidebar(inner=inner, counts=counts), body_class="app-inbox")
    )


async def projections(request: Request) -> HTMLResponse:
    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        items = store.list_projections(conn, status="active", limit=200)
    finally:
        conn.close()

    if not items:
        body_inner = (
            '<div class="list-empty">'
            '<div class="empty-title">No active projections</div>'
            "<div>Push commitments to Linear to see them here.</div>"
            "<code>verbatim project linear --team Engineering</code>"
            "</div>"
        )
    else:
        def render_proj(p: dict[str, Any]) -> str:
            ident = (p.get("metadata") or {}).get("identifier") or p.get("external_id") or ""
            url = p.get("external_url") or ""
            url_html = (
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="mono">{html.escape(url)}</a>'
                if url else '<span class="mono" style="color:var(--text-4)">—</span>'
            )
            return (
                '<div class="activity-item">'
                '<span class="activity-dot"></span>'
                '<div class="activity-body">'
                '<div class="activity-text">'
                f'<a href="/entity/{html.escape(p["entity_id"])}">'
                f'{html.escape(ident)}</a>'
                f' → {html.escape(p.get("primary_topic") or "")}'
                f'<div style="margin-top:4px">{url_html}</div>'
                "</div>"
                f'<div class="activity-when">{html.escape(p["created_at"][:19].replace("T", " "))} UTC</div>'
                "</div></div>"
            )

        rows = "".join(render_proj(p) for p in items)
        body_inner = f'<div class="activity-feed">{rows}</div>'

    inner = (
        '<main id="main-content" class="app-page">'
        '<h1 class="page-title">Projections</h1>'
        + body_inner
        + "</main>"
    )
    return HTMLResponse(
        _shell("Projections", _shell_with_sidebar(inner=inner, counts=counts),
               body_class="app-inbox")
    )


async def dashboard(request: Request) -> HTMLResponse:
    """Legacy dashboard view with stats + activity feed."""
    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        stats_dict = state.stats(conn)
        recent_sessions_data = state.recent_sessions(conn, limit=10)
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
        '<div class="stat">'
        f'<div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{value}</div>'
        "</div>"
        for label, value in stat_cards
    )

    activity_items = "".join(
        '<div class="activity-item">'
        '<span class="activity-dot"></span>'
        '<div class="activity-body">'
        f'<div class="activity-text">Ingested <strong>{s.get("entity_count", 0)}</strong> items from '
        f'<span class="mono">{html.escape(s.get("source_path") or "<stdin>")}</span></div>'
        f'<div class="activity-when">{html.escape(s["extracted_at"][:19].replace("T", " "))} UTC</div>'
        "</div></div>"
        for s in recent_sessions_data
    )
    activity_html = (
        f'<div class="activity-feed">{activity_items}</div>'
        if recent_sessions_data else ""
    )

    inner = (
        '<main id="main-content" class="app-page">'
        '<h1 class="page-title">Verbatim dashboard<span class="page-subtitle">overview</span></h1>'
        f'<div class="stat-grid">{stats_html}</div>'
        + ('<h2 class="section-h-page">Activity</h2>' + activity_html if recent_sessions_data else "")
        + "</main>"
    )
    return HTMLResponse(
        _shell("Dashboard", _shell_with_sidebar(inner=inner, counts=counts),
               body_class="app-inbox")
    )


# ----------------------- people + person detail -----------------------


def _render_person_section(
    title: str, items: list[dict[str, Any]], *, color_var: str
) -> str:
    if not items:
        return ""
    rows_html = "".join(_render_search_result(e, "") for e in items)
    return (
        '<div class="search-results-group">'
        f'<h3 style="color:var({color_var})">{html.escape(title)} '
        f'<span class="count">{len(items)}</span></h3>'
        f"{rows_html}"
        "</div>"
    )


async def people(request: Request) -> HTMLResponse:
    """List every distinct person who appears in the state graph."""
    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        items = store.list_known_people(conn, limit=300)
    finally:
        conn.close()

    if not items:
        list_html = (
            '<div class="list-empty">'
            '<div class="empty-title">No people yet</div>'
            "<div>Ingest a transcript to surface the people who appear in it.</div>"
            "<code>verbatim ingest path/to/meeting.txt</code>"
            "</div>"
        )
    else:
        rows = "".join(
            f'<a href="/person/{html.escape(p["name"])}" class="result-row">'
            f'<span class="result-icon">{_ICONS.get("inbox", "")}</span>'
            '<div class="result-body">'
            f'<div class="result-title">{html.escape(p["name"])}</div>'
            f'<div class="result-sub">{p["total"]} item{"s" if p["total"] != 1 else ""}</div>'
            "</div></a>"
            for p in items
        )
        list_html = (
            '<div class="search-results-group">'
            f'<h3>Known people <span class="count">{len(items)}</span></h3>'
            f"{rows}"
            "</div>"
        )

    inner = (
        '<main id="main-content" class="app-page">'
        '<h1 class="page-title">People'
        f'<span class="page-subtitle">{len(items)} known</span></h1>'
        + list_html
        + "</main>"
    )
    return HTMLResponse(
        _shell("People", _shell_with_sidebar(inner=inner, counts=counts),
               body_class="app-inbox")
    )


async def person_detail(request: Request) -> HTMLResponse:
    """Aggregated view of one person across all four entity kinds."""
    name = request.path_params["name"]
    conn = _open_conn()
    try:
        counts = _all_counts(conn)
        view = store.fetch_person(conn, name, include_resolved=False)
    finally:
        conn.close()

    stats = view["stats"]
    if stats["total"] == 0:
        inner = (
            '<main id="main-content" class="app-page">'
            f'<h1 class="page-title">{html.escape(name)}'
            '<span class="page-subtitle">no items</span></h1>'
            '<p style="color:var(--text-3)">Nothing recorded for this person. '
            '<a href="/people">Browse known people</a>.</p>'
            "</main>"
        )
    else:
        subtitle = (
            f'{stats["total"]} items · '
            f'{stats["commitments"]} commitment{"s" if stats["commitments"] != 1 else ""} · '
            f'{stats["decisions"]} decision{"s" if stats["decisions"] != 1 else ""} · '
            f'{stats["questions_raised"]} question{"s" if stats["questions_raised"] != 1 else ""} · '
            f'{stats["blockers_owned"]} blocker{"s" if stats["blockers_owned"] != 1 else ""}'
        )
        sections = "".join([
            _render_person_section("Commitments owed", view["commitments"],
                                   color_var="--commit"),
            _render_person_section("Blockers owned", view["blockers_owned"],
                                   color_var="--blocker"),
            _render_person_section("Questions raised", view["questions_raised"],
                                   color_var="--question"),
            _render_person_section("Decisions involved in", view["decisions"],
                                   color_var="--decision"),
        ])
        inner = (
            '<main id="main-content" class="app-page">'
            f'<h1 class="page-title">{html.escape(name)}'
            f'<span class="page-subtitle">{subtitle}</span></h1>'
            + sections
            + "</main>"
        )
    return HTMLResponse(
        _shell(f"{name} · People", _shell_with_sidebar(inner=inner, counts=counts),
               body_class="app-inbox")
    )


# ----------------------- app factory -----------------------


def create_app(db_path: Path | None = None) -> Starlette:
    _AppState.db_path = db_path
    routes = [
        Route("/", inbox),
        Route("/commitments", commitments),
        Route("/decisions", decisions),
        Route("/open-questions", open_questions),
        Route("/blockers", blockers),
        Route("/sessions", sessions),
        Route("/projections", projections),
        Route("/dashboard", dashboard),
        Route("/search", search_page),
        Route("/people", people),
        Route("/person/{name}", person_detail),
        Route("/entity/{entity_id}", entity_detail),
    ]
    return Starlette(routes=routes)


# ----------------------- nav links (kept for backwards compatibility) ------------

# Tests import _NAV_LINKS to assert link presence. Provide a synthetic equivalent
# so the existing assertions still match the new sidebar (which uses the same
# href → label mapping internally).
_NAV_LINKS: list[tuple[str, str, str]] = [
    ("/", "Inbox", "inbox"),
    ("/commitments", "Commitments", "commit"),
    ("/decisions", "Decisions", "decision"),
    ("/open-questions", "Questions", "question"),
    ("/blockers", "Blockers", "blocker"),
    ("/people", "People", "inbox"),
    ("/sessions", "Sessions", "meeting"),
    ("/projections", "Projections", "bolt"),
]
