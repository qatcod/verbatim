"""Email digest — the third consumer surface, for users who live in email.

Same digest content as the Slack `post_digest` and the web dashboard, rendered
as multipart MIME (plain text + HTML) and shipped via SMTP. Designed for cron
("Monday 9am team digest") rather than interactive use.

# Config

SMTP settings come from CLI args first, env vars second:

    --smtp-host / $SMTP_HOST            (e.g. smtp.gmail.com, smtp.sendgrid.net)
    --smtp-port / $SMTP_PORT            (default 587 for STARTTLS, 465 for SSL)
    --smtp-user / $SMTP_USER
    --smtp-password / $SMTP_PASSWORD
    --from     / $SMTP_FROM             From: header
    --to       (required)               To: header — one or more recipients

# Why not SendGrid/SES SDKs

SMTP is universal — every email provider supports it. Adding provider SDKs
would pull bigger dependencies for marginal benefit. Users with SES can use
SES's SMTP interface; users with SendGrid use SendGrid's. Same code path.
"""
from __future__ import annotations

import html
import smtplib
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from . import state

# ----------------------- digest content rendering -----------------------


@dataclass
class DigestContent:
    """Rendered digest payload — both formats produced in one pass for parity."""

    subject: str
    text: str
    html: str


def render_digest(conn: sqlite3.Connection, *, brand: str = "Verbatim") -> DigestContent:
    """Build a digest of current state, in both text and HTML."""
    stats_dict = state.stats(conn)
    commitments = state.list_commitments(conn, limit=10)
    blockers = state.list_blockers(conn, limit=10)
    questions = state.list_open_questions(conn, limit=10)

    counts_line = (
        f"{stats_dict.get('commitments_open', 0)} commitments · "
        f"{stats_dict.get('decisions_open', 0)} decisions · "
        f"{stats_dict.get('open_questions_open', 0)} questions · "
        f"{stats_dict.get('blockers_open', 0)} blockers"
    )

    subject = f"[{brand}] state digest — {counts_line}"
    return DigestContent(
        subject=subject,
        text=_render_text(stats_dict, commitments, blockers, questions, brand=brand),
        html=_render_html(stats_dict, commitments, blockers, questions, brand=brand),
    )


def _render_text(
    stats_dict: dict[str, int],
    commitments: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    *,
    brand: str,
) -> str:
    lines: list[str] = [
        f"{brand} state digest",
        "=" * (len(brand) + 14),
        "",
        f"Sessions ingested:  {stats_dict.get('sessions', 0)}",
        f"Open commitments:   {stats_dict.get('commitments_open', 0)}",
        f"Decisions:          {stats_dict.get('decisions_open', 0)}",
        f"Open questions:     {stats_dict.get('open_questions_open', 0)}",
        f"Blockers:           {stats_dict.get('blockers_open', 0)}",
        f"Entities merged:    {stats_dict.get('entities_merged', 0)}",
        f"Active projections: {stats_dict.get('projections_active', 0)}",
        "",
    ]

    if commitments:
        lines.append("Recent commitments")
        lines.append("------------------")
        for c in commitments:
            p = c["payload"]
            deadline = f"  (by {p['deadline']})" if p.get("deadline") else ""
            lines.append(f"  - [{c['confidence']}] {p.get('actor') or '?'}: {p.get('deliverable') or '?'}{deadline}")
        lines.append("")

    if blockers:
        lines.append("Open blockers")
        lines.append("-------------")
        for b in blockers:
            p = b["payload"]
            owner = f"  (owner: {p['owner']})" if p.get("owner") else ""
            lines.append(
                f"  - [{b['confidence']}] {p.get('blocked_thing') or '?'} "
                f"— blocked by {p.get('blocked_by') or '?'}{owner}"
            )
        lines.append("")

    if questions:
        lines.append("Open questions")
        lines.append("--------------")
        for q in questions:
            p = q["payload"]
            raised = f"  (raised by {p['raised_by']})" if p.get("raised_by") else ""
            lines.append(f"  - [{q['confidence']}] {p.get('topic') or '?'}: {p.get('question') or '?'}{raised}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


_HTML_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif;
       color: #1a1a1a; background: #fafafa; margin: 0; padding: 24px; }
.container { max-width: 640px; margin: 0 auto; background: #fff;
             border: 1px solid #e5e7eb; border-radius: 8px; padding: 24px; }
h1 { font-size: 20px; margin: 0 0 16px; }
h2 { font-size: 14px; margin: 24px 0 8px; text-transform: uppercase;
     letter-spacing: 0.5px; color: #6b7280; }
.stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 24px;
         margin-bottom: 16px; font-size: 14px; }
.stats .label { color: #6b7280; }
.item { padding: 8px 0; border-bottom: 1px solid #f3f4f6; font-size: 14px; }
.item:last-child { border-bottom: none; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 9999px;
         font-size: 11px; margin-right: 6px; }
.badge.high { background: #dcfce7; color: #16a34a; }
.badge.medium { background: #fef3c7; color: #ca8a04; }
.badge.low { background: #fee2e2; color: #dc2626; }
.muted { color: #6b7280; font-size: 12px; }
"""


def _render_html(
    stats_dict: dict[str, int],
    commitments: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    *,
    brand: str,
) -> str:
    def confidence_html(c: str) -> str:
        return f'<span class="badge {html.escape(c)}">{html.escape(c)}</span>'

    stat_pairs = [
        ("Sessions", stats_dict.get("sessions", 0)),
        ("Open commitments", stats_dict.get("commitments_open", 0)),
        ("Decisions", stats_dict.get("decisions_open", 0)),
        ("Open questions", stats_dict.get("open_questions_open", 0)),
        ("Blockers", stats_dict.get("blockers_open", 0)),
        ("Merged entities", stats_dict.get("entities_merged", 0)),
        ("Active projections", stats_dict.get("projections_active", 0)),
    ]
    stats_html = "".join(
        f'<div><span class="label">{html.escape(label)}:</span> '
        f'<strong>{value}</strong></div>'
        for label, value in stat_pairs
    )

    sections: list[str] = []
    if commitments:
        items = ""
        for c in commitments:
            p = c["payload"]
            deadline = f' <span class="muted">by {html.escape(p["deadline"])}</span>' if p.get("deadline") else ""
            items += (
                f'<div class="item">{confidence_html(c["confidence"])}'
                f'<strong>{html.escape(p.get("actor") or "?")}</strong>: '
                f'{html.escape(p.get("deliverable") or "?")}{deadline}</div>'
            )
        sections.append(f"<h2>Recent commitments</h2>{items}")

    if blockers:
        items = ""
        for b in blockers:
            p = b["payload"]
            owner = f' <span class="muted">(owner: {html.escape(p["owner"])})</span>' if p.get("owner") else ""
            items += (
                f'<div class="item">{confidence_html(b["confidence"])}'
                f'<strong>{html.escape(p.get("blocked_thing") or "?")}</strong>'
                f' blocked by <strong>{html.escape(p.get("blocked_by") or "?")}</strong>{owner}</div>'
            )
        sections.append(f"<h2>Open blockers</h2>{items}")

    if questions:
        items = ""
        for q in questions:
            p = q["payload"]
            raised = f' <span class="muted">(raised by {html.escape(p["raised_by"])})</span>' if p.get("raised_by") else ""
            items += (
                f'<div class="item">{confidence_html(q["confidence"])}'
                f'<strong>{html.escape(p.get("topic") or "?")}</strong>: '
                f'{html.escape(p.get("question") or "?")}{raised}</div>'
            )
        sections.append(f"<h2>Open questions</h2>{items}")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_HTML_STYLE}</style></head>
<body><div class="container">
<h1>{html.escape(brand)} state digest</h1>
<div class="stats">{stats_html}</div>
{''.join(sections)}
</div></body></html>"""


# ----------------------- email sending -----------------------


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    use_ssl: bool = False  # True = SMTPS (465), False = STARTTLS (587/25)


def build_message(
    content: DigestContent,
    *,
    sender: str,
    recipients: list[str],
    sender_name: str | None = None,
) -> EmailMessage:
    """Build a multipart MIME message with text and HTML parts."""
    msg = EmailMessage()
    msg["Subject"] = content.subject
    msg["From"] = formataddr((sender_name or "Verbatim", sender)) if sender_name else sender
    msg["To"] = ", ".join(recipients)
    msg["Message-ID"] = make_msgid(domain="verbatim.local")
    msg.set_content(content.text)
    msg.add_alternative(content.html, subtype="html")
    return msg


def send_via_smtp(message: EmailMessage, smtp: SmtpConfig) -> None:
    """Send an email through an SMTP server. Synchronous; raises on failure."""
    cls = smtplib.SMTP_SSL if smtp.use_ssl else smtplib.SMTP
    with cls(smtp.host, smtp.port) as server:
        if not smtp.use_ssl:
            server.starttls()
        if smtp.username and smtp.password:
            server.login(smtp.username, smtp.password)
        server.send_message(message)
