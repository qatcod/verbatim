"""Email digest tests — rendering, MIME structure, SMTP send (mocked)."""
from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from verbatim import email_digest, state
from verbatim.extractor import ExtractionDiagnostics
from verbatim.schema import (
    Blocker,
    Commitment,
    Confidence,
    Decision,
    ExtractionResult,
    OpenQuestion,
    SourceReference,
)


@pytest.fixture
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    result = ExtractionResult(
        meeting_summary="digest seed",
        participants=["Alice"],
        commitments=[Commitment(
            actor="Alice", deliverable="ship v0", deadline="Friday",
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="x", speaker="Alice", rationale="r")],
        )],
        decisions=[Decision(
            topic="lang", outcome="Python", participants=["Alice"],
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="y", speaker="Alice", rationale="r")],
        )],
        open_questions=[OpenQuestion(
            topic="cost", question="What's the budget?",
            raised_by="Carol", confidence=Confidence.MEDIUM,
            sources=[SourceReference(verbatim_quote="z", speaker="Carol", rationale="r")],
        )],
        blockers=[Blocker(
            blocked_thing="ship public", blocked_by="review",
            owner="Carol", confidence=Confidence.LOW,
            sources=[SourceReference(verbatim_quote="w", speaker="Bob", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path="m.txt")
    return conn


# ----- render_digest -----


def test_render_digest_returns_all_three_fields(seeded_conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(seeded_conn)
    assert content.subject
    assert content.text
    assert content.html


def test_render_digest_subject_includes_counts(seeded_conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(seeded_conn)
    assert "1 commitments" in content.subject
    assert "1 decisions" in content.subject
    assert "1 questions" in content.subject
    assert "1 blockers" in content.subject


def test_render_digest_text_contains_each_section(seeded_conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(seeded_conn)
    text = content.text
    assert "Recent commitments" in text
    assert "Open blockers" in text
    assert "Open questions" in text
    assert "Alice" in text
    assert "ship v0" in text


def test_render_digest_html_contains_each_section(seeded_conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(seeded_conn)
    html_body = content.html
    assert "<!DOCTYPE html>" in html_body
    assert "ship v0" in html_body
    # confidence badges
    assert "badge high" in html_body
    assert "badge medium" in html_body
    assert "badge low" in html_body


def test_render_digest_brand_threads_through(seeded_conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(seeded_conn, brand="DataCorp")
    assert "DataCorp" in content.subject
    assert "DataCorp" in content.text
    assert "DataCorp" in content.html


def test_render_digest_empty_state(conn: sqlite3.Connection) -> None:
    content = email_digest.render_digest(conn)
    assert "0 commitments" in content.subject
    # No "Recent commitments" section when empty
    assert "Recent commitments" not in content.text


def test_render_digest_html_escapes_user_content(conn: sqlite3.Connection) -> None:
    result = ExtractionResult(
        meeting_summary="x", participants=[],
        commitments=[Commitment(
            actor="<script>", deliverable="x</td><script>",
            confidence=Confidence.HIGH,
            sources=[SourceReference(verbatim_quote="q", speaker="s", rationale="r")],
        )],
    )
    diag = ExtractionDiagnostics(
        model="t", input_tokens=1, output_tokens=1,
        stop_reason="end_turn", transcript_chars=10,
    )
    state.save_extraction(conn, result, diag, source_path=None)
    content = email_digest.render_digest(conn)
    assert "<script>" not in content.html
    assert "&lt;script&gt;" in content.html


# ----- build_message -----


def test_build_message_is_multipart_text_html() -> None:
    content = email_digest.DigestContent(
        subject="Test subject", text="plain body", html="<p>html body</p>",
    )
    msg = email_digest.build_message(
        content, sender="bot@x.com", recipients=["a@x.com"], sender_name="Verbatim Bot",
    )
    assert msg["Subject"] == "Test subject"
    assert "bot@x.com" in msg["From"]
    assert msg["To"] == "a@x.com"
    # multipart with two parts: text/plain + text/html
    parts = list(msg.walk())
    types = [p.get_content_type() for p in parts]
    assert "text/plain" in types
    assert "text/html" in types


def test_build_message_handles_multiple_recipients() -> None:
    content = email_digest.DigestContent(subject="s", text="t", html="<p>h</p>")
    msg = email_digest.build_message(
        content, sender="bot@x.com", recipients=["a@x.com", "b@x.com"],
    )
    assert "a@x.com" in msg["To"]
    assert "b@x.com" in msg["To"]


def test_build_message_includes_message_id() -> None:
    content = email_digest.DigestContent(subject="s", text="t", html="<p>h</p>")
    msg = email_digest.build_message(
        content, sender="bot@x.com", recipients=["a@x.com"],
    )
    assert msg["Message-ID"] is not None


# ----- send_via_smtp -----


def test_send_via_smtp_uses_starttls_by_default() -> None:
    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    cfg = email_digest.SmtpConfig(
        host="smtp.x.com", port=587,
        username="u", password="p", use_ssl=False,
    )
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg.set_content("hi")

    with patch("verbatim.email_digest.smtplib.SMTP", smtp_class):
        email_digest.send_via_smtp(msg, cfg)

    smtp_class.assert_called_once_with("smtp.x.com", 587)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("u", "p")
    smtp_instance.send_message.assert_called_once_with(msg)


def test_send_via_smtp_uses_ssl_when_requested() -> None:
    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    cfg = email_digest.SmtpConfig(
        host="smtp.x.com", port=465, username="u", password="p", use_ssl=True,
    )
    msg = EmailMessage()
    msg.set_content("hi")

    with patch("verbatim.email_digest.smtplib.SMTP_SSL", smtp_class):
        email_digest.send_via_smtp(msg, cfg)

    smtp_class.assert_called_once_with("smtp.x.com", 465)
    smtp_instance.starttls.assert_not_called()
    smtp_instance.login.assert_called_once_with("u", "p")


def test_send_via_smtp_skips_login_without_credentials() -> None:
    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    cfg = email_digest.SmtpConfig(
        host="smtp.x.com", port=587, username=None, password=None, use_ssl=False,
    )
    msg = EmailMessage()
    msg.set_content("hi")

    with patch("verbatim.email_digest.smtplib.SMTP", smtp_class):
        email_digest.send_via_smtp(msg, cfg)

    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_called_once_with(msg)
