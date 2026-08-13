"""Tests for app/core/extraction/email.py::parse_message() -- the
structured email parse Communications Phase Step 3 uses for manual .eml
upload (and, later, IMAP-fetched messages). Distinct from `extract()`,
which serves the pre-existing Document extraction pipeline and is left
untouched.
"""

from __future__ import annotations

from pathlib import Path

from app.core.extraction.email import parse_message


def _write_eml(tmp_path: Path, raw: bytes, name: str = "message.eml") -> Path:
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def test_parses_headers_and_body(tmp_path: Path):
    raw = (
        b'From: "Amanda Wagner" <amanda.wagner@district.example.org>\n'
        b"To: Parent One <parent@yahoo.com>, \"Parent Two\" <parent2@yahoo.com>\n"
        b"Cc: teacher@district.example.org\n"
        b"Subject: Re: IEP Meeting Follow-up\n"
        b"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        b"Message-ID: <abc123@district.example.org>\n"
        b"In-Reply-To: <original456@yahoo.com>\n"
        b"References: <original456@yahoo.com> <followup789@district.example.org>\n"
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"\n"
        b"This is the body of the email.\n"
    )
    parsed = parse_message(_write_eml(tmp_path, raw))

    assert parsed.subject == "Re: IEP Meeting Follow-up"
    assert parsed.from_address == "amanda.wagner@district.example.org"
    assert parsed.from_display_name == "Amanda Wagner"
    assert parsed.to_addresses == ["parent@yahoo.com", "parent2@yahoo.com"]
    assert parsed.cc_addresses == ["teacher@district.example.org"]
    assert parsed.bcc_addresses == []
    assert parsed.sent_at is not None
    assert parsed.sent_at.year == 2022 and parsed.sent_at.month == 3 and parsed.sent_at.day == 7
    assert parsed.message_id == "<abc123@district.example.org>"
    assert parsed.in_reply_to == "<original456@yahoo.com>"
    assert parsed.references == ["<original456@yahoo.com>", "<followup789@district.example.org>"]
    assert parsed.body_text == "This is the body of the email."
    assert parsed.body_html is None
    assert parsed.attachments == []


def test_raw_headers_preserves_every_header_in_order(tmp_path: Path):
    raw = (
        b"From: sender@example.org\n"
        b"To: recipient@example.org\n"
        b"Subject: Hello\n"
        b"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        b"\n"
        b"Body text.\n"
    )
    parsed = parse_message(_write_eml(tmp_path, raw))
    names = [h["name"] for h in parsed.raw_headers]
    assert names == ["From", "To", "Subject", "Date"]
    assert {"name": "Subject", "value": "Hello"} in parsed.raw_headers


def test_missing_optional_headers_yield_none_or_empty(tmp_path: Path):
    raw = b"Subject: No sender at all\n\nJust a body.\n"
    parsed = parse_message(_write_eml(tmp_path, raw))

    assert parsed.from_address is None
    assert parsed.from_display_name is None
    assert parsed.to_addresses == []
    assert parsed.cc_addresses == []
    assert parsed.sent_at is None
    assert parsed.message_id is None
    assert parsed.in_reply_to is None
    assert parsed.references == []
    assert parsed.subject == "No sender at all"
    assert parsed.body_text == "Just a body."


def test_bcc_only_populated_when_actually_present(tmp_path: Path):
    raw = (
        b"From: sender@example.org\n"
        b"To: recipient@example.org\n"
        b"Bcc: secret@example.org\n"
        b"Subject: Has a Bcc\n\n"
        b"Body.\n"
    )
    parsed = parse_message(_write_eml(tmp_path, raw))
    assert parsed.bcc_addresses == ["secret@example.org"]


def test_html_only_body_populates_html_not_text(tmp_path: Path):
    raw = (
        b"From: sender@example.org\n"
        b"To: recipient@example.org\n"
        b"Subject: HTML only\n"
        b'Content-Type: text/html; charset="utf-8"\n\n'
        b"<p>Hello <b>world</b></p>\n"
    )
    parsed = parse_message(_write_eml(tmp_path, raw))
    assert parsed.body_text is None
    assert parsed.body_html is not None
    assert "<p>" in parsed.body_html


def test_multiple_to_headers_are_all_collected(tmp_path: Path):
    """A message can legally carry more than one occurrence of the same
    header name -- every address across every occurrence must be
    collected, not just the first.
    """
    raw = (
        b"From: sender@example.org\n"
        b"To: first@example.org\n"
        b"To: second@example.org\n"
        b"Subject: Two To headers\n\n"
        b"Body.\n"
    )
    parsed = parse_message(_write_eml(tmp_path, raw))
    assert set(parsed.to_addresses) == {"first@example.org", "second@example.org"}


def test_never_modifies_the_source_file(tmp_path: Path):
    raw = b"From: sender@example.org\nSubject: Untouched\n\nBody.\n"
    path = _write_eml(tmp_path, raw)
    original_bytes = path.read_bytes()

    parse_message(path)

    assert path.read_bytes() == original_bytes
