"""Email-aware Document metadata suggestions for a communication
attachment (Communications Phase Step 5).

Deliberately *not* built on `app/core/document_source_suggestion.py` or
`app/core/document_date_suggestion.py` -- those modules exist to scrape a
Source/date out of unstructured text via regex, which is exactly the
wrong tool here: a `Communication` already carries structured,
already-parsed From/sent/received fields (see
`app/core/extraction/email.py::parse_message`), which is strictly
higher-confidence evidence than re-deriving the same information from
text. This module turns that already-structured provenance into the
same kind of suggestion object the drop-zone auto-fill flow already
understands -- a plain value the review form pre-fills and the human can
edit or clear before anything is ingested (see
`app/api/communications.py`'s attachment-review route).
"""

from __future__ import annotations

from datetime import date

from app.db.models import Communication


def suggest_source(communication: Communication) -> str | None:
    """A high-confidence Source suggestion from the email's structured
    From header: "Display Name <address>" when both are known, else
    whichever one is present, else None.
    """
    name = (communication.from_display_name or "").strip()
    address = (communication.from_address or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return name or address or None


def suggest_date_received(communication: Communication) -> date | None:
    """The date this attachment can be treated as received, or None.

    Only suggested when the email itself establishes receipt: its
    `received_at` (a mailbox-observed timestamp -- not present for a
    manually uploaded `.eml`, since there's no mailbox to observe it in)
    if known, else its `sent_at` -- a message's own Date: header is still
    direct evidence of when the family received this copy, since a
    preserved `.eml` file is, by definition, mail the family already has.
    Never a guess derived from anything else (e.g. never the import
    timestamp, which only reflects when this application happened to see
    the file, not when the family actually received it).
    """
    moment = communication.received_at or communication.sent_at
    return moment.date() if moment is not None else None


def _sender_label(communication: Communication) -> str:
    name = (communication.from_display_name or "").strip()
    address = (communication.from_address or "").strip()
    if name:
        return name
    return address or "an unknown sender"


def compose_notes(communication: Communication) -> str:
    """A fixed-template, deterministic note -- never interpretive or
    AI-written prose, matching `app/core/document_notes_suggestion.py`'s
    same "compose from known facts, don't summarize" philosophy.
    """
    sender = _sender_label(communication)
    received = suggest_date_received(communication)
    when = received.isoformat() if received is not None else "an unknown date"
    return f"Received as an attachment to email from {sender} on {when}."
