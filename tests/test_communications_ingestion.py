"""Tests for app/core/communications/ingestion.py -- manual .eml import
(Communications Phase Step 3).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.ingestion import DuplicateCommunicationError, import_eml_file
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import Case, Communication, CommunicationAttachment, CommunicationCustodyEvent


def _eml_bytes(
    *,
    message_id: str = "<msg-1@example.org>",
    subject: str = "IEP Meeting Notice",
    from_addr: str = "amanda.wagner@district.example.org",
    body: str = "Please see the attached notice.",
) -> bytes:
    return (
        f"From: Amanda Wagner <{from_addr}>\n"
        f"To: parent@yahoo.com\n"
        f"Subject: {subject}\n"
        f"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        f"Message-ID: {message_id}\n"
        f"\n"
        f"{body}\n"
    ).encode("utf-8")


def _write_eml(tmp_path: Path, raw: bytes, name: str = "notice.eml") -> Path:
    path = tmp_path / "source-files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def test_import_creates_communication_and_records_hash(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write_eml(tmp_path, _eml_bytes())
    original_hash = compute_sha256(source)

    communication = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="notice.eml", actor="test-user"
    )
    db_session.commit()

    assert communication.communication_id is not None
    assert communication.sha256_hash == original_hash
    assert communication.case_id == sample_case.case_id
    assert communication.account_id is None
    assert communication.import_method == "manual_upload"
    assert communication.subject == "IEP Meeting Notice"
    assert communication.from_address == "amanda.wagner@district.example.org"

    stored_path = vault.root / communication.stored_path
    assert stored_path.exists()
    assert stored_path.read_bytes() == source.read_bytes()


def test_import_never_modifies_the_source_file(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write_eml(tmp_path, _eml_bytes())
    original_content = source.read_bytes()
    original_hash = compute_sha256(source)

    import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="notice.eml", actor="test-user"
    )
    db_session.commit()

    assert source.read_bytes() == original_content
    assert compute_sha256(source) == original_hash


def test_imported_file_is_read_only_in_the_vault(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write_eml(tmp_path, _eml_bytes())
    communication = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="notice.eml", actor="test-user"
    )
    db_session.commit()

    stored_path = vault.root / communication.stored_path
    mode = stored_path.stat().st_mode
    assert not (mode & stat.S_IWUSR)


def test_import_records_imported_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write_eml(tmp_path, _eml_bytes())
    communication = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="notice.eml", actor="test-user"
    )
    db_session.commit()

    events = db_session.scalars(
        select(CommunicationCustodyEvent).where(
            CommunicationCustodyEvent.communication_id == communication.communication_id
        )
    ).all()
    assert len(events) == 1
    assert events[0].event_type == "imported"
    assert events[0].actor == "test-user"
    assert events[0].sha256_hash_at_event == communication.sha256_hash


def test_duplicate_message_id_is_rejected(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source_a = _write_eml(tmp_path, _eml_bytes(message_id="<dup@example.org>"), name="a.eml")
    import_eml_file(
        db_session, vault, sample_case, source_file_path=source_a, original_filename="a.eml", actor="test-user"
    )
    db_session.commit()

    # Different bytes, but the same Message-ID -- still a duplicate by
    # the primary dedup key.
    source_b = _write_eml(
        tmp_path, _eml_bytes(message_id="<dup@example.org>", body="different body text"), name="b.eml"
    )
    with pytest.raises(DuplicateCommunicationError):
        import_eml_file(
            db_session, vault, sample_case, source_file_path=source_b, original_filename="b.eml", actor="test-user"
        )

    assert db_session.query(Communication).count() == 1


def test_duplicate_hash_with_no_message_id_is_rejected(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    raw = b"From: sender@example.org\nSubject: No Message-ID\n\nSame body every time.\n"
    source_a = _write_eml(tmp_path, raw, name="a.eml")
    import_eml_file(
        db_session, vault, sample_case, source_file_path=source_a, original_filename="a.eml", actor="test-user"
    )
    db_session.commit()

    # Identical bytes (same hash), also no Message-ID -- falls back to
    # the sha256 dedup key.
    source_b = _write_eml(tmp_path, raw, name="b.eml")
    with pytest.raises(DuplicateCommunicationError):
        import_eml_file(
            db_session, vault, sample_case, source_file_path=source_b, original_filename="b.eml", actor="test-user"
        )

    assert db_session.query(Communication).count() == 1


def test_same_content_different_message_id_is_not_a_duplicate(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source_a = _write_eml(tmp_path, _eml_bytes(message_id="<one@example.org>"), name="a.eml")
    import_eml_file(
        db_session, vault, sample_case, source_file_path=source_a, original_filename="a.eml", actor="test-user"
    )
    db_session.commit()

    source_b = _write_eml(tmp_path, _eml_bytes(message_id="<two@example.org>"), name="b.eml")
    import_eml_file(
        db_session, vault, sample_case, source_file_path=source_b, original_filename="b.eml", actor="test-user"
    )
    db_session.commit()

    assert db_session.query(Communication).count() == 2


def test_attachments_are_stored_hashed_and_pending_review(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    raw = (
        b"From: sender@example.org\n"
        b"To: parent@yahoo.com\n"
        b"Subject: Has an attachment\n"
        b"Message-ID: <att-1@example.org>\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY"\n'
        b"\n"
        b"--BOUNDARY\n"
        b"Content-Type: text/plain\n\n"
        b"See attached.\n"
        b"--BOUNDARY\n"
        b"Content-Type: application/pdf\n"
        b"Content-Disposition: attachment; filename=\"2023 Annual IEP.pdf\"\n"
        b"Content-Transfer-Encoding: base64\n\n"
        b"JVBERi0xLjQK\n"  # arbitrary bytes, not a real PDF -- irrelevant to this test
        b"--BOUNDARY--\n"
    )
    source = _write_eml(tmp_path, raw)

    communication = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="notice.eml", actor="test-user"
    )
    db_session.commit()

    attachments = db_session.scalars(
        select(CommunicationAttachment).where(
            CommunicationAttachment.communication_id == communication.communication_id
        )
    ).all()
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.filename == "2023 Annual IEP.pdf"
    assert attachment.review_status == "pending"
    assert attachment.resulting_document_id is None
    assert len(attachment.sha256_hash) == 64

    stored_path = vault.root / attachment.stored_path
    assert stored_path.exists()
    mode = stored_path.stat().st_mode
    assert not (mode & stat.S_IWUSR)
