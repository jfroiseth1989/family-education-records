"""Tests for app/core/communications/attachment_metadata.py
(Communications Phase Step 5): email-aware Source/Date Received/Notes
suggestions built from a Communication's already-structured fields.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.communications.attachment_metadata import (
    compose_notes,
    suggest_date_received,
    suggest_source,
)
from app.db.models import Communication


def _communication(**overrides) -> Communication:
    defaults = dict(
        communication_type="email",
        subject="IEP Meeting Notice",
        from_display_name="Amanda Wagner",
        from_address="amanda.wagner@district.example.org",
        sent_at=datetime(2022, 3, 7, 14, 30, tzinfo=timezone.utc),
        received_at=None,
        sha256_hash="a" * 64,
        stored_path="p",
        file_size_bytes=1,
        import_method="manual_upload",
        imported_by="test-user",
    )
    defaults.update(overrides)
    return Communication(**defaults)


def test_suggest_source_uses_display_name_and_address():
    communication = _communication()
    assert suggest_source(communication) == "Amanda Wagner <amanda.wagner@district.example.org>"


def test_suggest_source_falls_back_to_address_only():
    communication = _communication(from_display_name=None)
    assert suggest_source(communication) == "amanda.wagner@district.example.org"


def test_suggest_source_falls_back_to_display_name_only():
    communication = _communication(from_address=None)
    assert suggest_source(communication) == "Amanda Wagner"


def test_suggest_source_none_when_nothing_known():
    communication = _communication(from_display_name=None, from_address=None)
    assert suggest_source(communication) is None


def test_suggest_date_received_prefers_received_at():
    communication = _communication(
        sent_at=datetime(2022, 3, 7, tzinfo=timezone.utc),
        received_at=datetime(2022, 3, 8, tzinfo=timezone.utc),
    )
    assert suggest_date_received(communication).isoformat() == "2022-03-08"


def test_suggest_date_received_falls_back_to_sent_at():
    communication = _communication(sent_at=datetime(2022, 3, 7, tzinfo=timezone.utc), received_at=None)
    assert suggest_date_received(communication).isoformat() == "2022-03-07"


def test_suggest_date_received_none_when_neither_known():
    communication = _communication(sent_at=None, received_at=None)
    assert suggest_date_received(communication) is None


def test_compose_notes_deterministic_sentence():
    communication = _communication()
    assert compose_notes(communication) == (
        "Received as an attachment to email from Amanda Wagner on 2022-03-07."
    )


def test_compose_notes_uses_address_when_no_display_name():
    communication = _communication(from_display_name=None)
    assert compose_notes(communication) == (
        "Received as an attachment to email from amanda.wagner@district.example.org on 2022-03-07."
    )


def test_compose_notes_handles_unknown_sender_and_date():
    communication = _communication(from_display_name=None, from_address=None, sent_at=None, received_at=None)
    assert compose_notes(communication) == "Received as an attachment to email from an unknown sender on an unknown date."
