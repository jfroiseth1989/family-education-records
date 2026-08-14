"""Service-level tests for app/core/communications/promotion.py
(Communications Phase Step 5): promoting an attachment to a Document,
duplicate-document reuse via CommunicationDocumentLink, idempotent
re-linking, and exclude/leave-with-email review outcomes.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.ingestion import import_eml_file
from app.core.communications.promotion import (
    AttachmentNotPromotableError,
    exclude_attachment,
    leave_attachment_with_email,
    promote_attachment_to_document,
)
from app.core.files import compute_sha256
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import (
    Case,
    Communication,
    CommunicationAttachment,
    CommunicationCustodyEvent,
    CommunicationDocumentLink,
    Document,
    DocumentCustodyEvent,
)


def _eml_with_pdf_attachment(
    *, message_id: str, subject: str = "IEP Meeting Notice", filename: str = "2024 IEP.pdf"
) -> bytes:
    return (
        b"From: Amanda Wagner <amanda.wagner@district.example.org>\n"
        b"To: parent@yahoo.com\n"
        b"Subject: " + subject.encode() + b"\n"
        b"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        b"Message-ID: " + message_id.encode() + b"\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY"\n'
        b"\n"
        b"--BOUNDARY\n"
        b"Content-Type: text/plain\n\n"
        b"See attached.\n"
        b"--BOUNDARY\n"
        b"Content-Type: application/pdf\n"
        b'Content-Disposition: attachment; filename="' + filename.encode() + b'"\n'
        b"Content-Transfer-Encoding: base64\n\n"
        b"JVBERi0xLjQK\n"
        b"--BOUNDARY--\n"
    )


def _import_with_attachment(
    db: Session, vault: VaultLayout, case: Case, tmp_path: Path, *, message_id: str, name: str = "notice.eml", **kwargs
) -> tuple[Communication, CommunicationAttachment]:
    source = tmp_path / "source-files" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_eml_with_pdf_attachment(message_id=message_id, **kwargs))
    communication = import_eml_file(
        db, vault, case, source_file_path=source, original_filename=name, actor="test-user"
    )
    db.commit()
    attachment = db.scalars(
        select(CommunicationAttachment).where(CommunicationAttachment.communication_id == communication.communication_id)
    ).one()
    return communication, attachment


# --- promotion: new document -----------------------------------------------


def test_promote_creates_new_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<p1@x>"
    )

    result = promote_attachment_to_document(
        db_session,
        vault,
        attachment,
        document_type_id=None,
        source="Amanda Wagner",
        notes="Received via email.",
        date_received=None,
        field_provenance=None,
        actor="test-user",
    )
    db_session.commit()

    assert result.created_new_document is True
    assert result.already_linked is False
    assert result.document.case_id == sample_case.case_id
    assert result.document.original_filename == "2024 IEP.pdf"
    assert result.document.source == "Amanda Wagner"

    db_session.refresh(attachment)
    assert attachment.resulting_document_id == result.document.document_id
    assert attachment.review_status == "added_to_documents"


def test_promoted_document_has_communication_link(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<p2@x>"
    )

    result = promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    link = db_session.scalars(select(CommunicationDocumentLink)).one()
    assert link.document_id == result.document.document_id
    assert link.communication_id == communication.communication_id
    assert link.communication_attachment_id == attachment.attachment_id


def test_attachment_bytes_and_hash_unchanged_after_promotion(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<p3@x>"
    )
    attachment_full_path = vault.root / attachment.stored_path
    original_bytes = attachment_full_path.read_bytes()
    original_hash = attachment.sha256_hash

    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    # The attachment's own preserved copy is untouched -- still there,
    # still read-only, still byte-identical.
    assert attachment_full_path.exists()
    assert attachment_full_path.read_bytes() == original_bytes
    assert compute_sha256(attachment_full_path) == original_hash
    mode = attachment_full_path.stat().st_mode
    assert not (mode & stat.S_IWUSR)
    db_session.refresh(attachment)
    assert attachment.sha256_hash == original_hash


def test_raw_email_unchanged_after_promotion(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<p4@x>"
    )
    email_full_path = vault.root / communication.stored_path
    original_hash = communication.sha256_hash
    original_bytes = email_full_path.read_bytes()

    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    assert email_full_path.read_bytes() == original_bytes
    db_session.refresh(communication)
    assert communication.sha256_hash == original_hash


# --- duplicate-document behavior --------------------------------------------


def test_duplicate_attachment_links_to_existing_document_not_a_new_one(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    # Ingest the same content directly as a Document first (simulating it
    # already being in FERChronos from some other path).
    existing_source = tmp_path / "source-files" / "existing.pdf"
    existing_source.parent.mkdir(parents=True, exist_ok=True)
    import base64

    existing_source.write_bytes(base64.b64decode(b"JVBERi0xLjQK"))
    existing_document = ingest_document(
        db_session, vault, sample_case, source_file_path=existing_source,
        original_filename="2024 IEP.pdf", actor="test-user",
    )
    db_session.commit()

    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<dup1@x>"
    )

    result = promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    assert result.created_new_document is False
    assert result.document.document_id == existing_document.document_id
    assert db_session.query(Document).count() == 1

    link = db_session.scalars(select(CommunicationDocumentLink)).one()
    assert link.document_id == existing_document.document_id


def test_duplicate_promotion_does_not_overwrite_existing_document_fields(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    existing_source = tmp_path / "source-files" / "existing.pdf"
    existing_source.parent.mkdir(parents=True, exist_ok=True)
    import base64

    existing_source.write_bytes(base64.b64decode(b"JVBERi0xLjQK"))
    existing_document = ingest_document(
        db_session, vault, sample_case, source_file_path=existing_source,
        original_filename="2024 IEP.pdf", actor="test-user",
        source="Original Manual Entry", notes="Manually entered notes.",
    )
    db_session.commit()

    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<dup2@x>"
    )

    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None,
        source="Should Not Overwrite", notes="Should not overwrite either.",
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    db_session.refresh(existing_document)
    assert existing_document.source == "Original Manual Entry"
    assert existing_document.notes == "Manually entered notes."


def test_two_emails_same_attachment_one_document_two_links(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication_a, attachment_a = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<multi-a@x>", name="a.eml"
    )
    communication_b, attachment_b = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<multi-b@x>", name="b.eml"
    )

    result_a = promote_attachment_to_document(
        db_session, vault, attachment_a, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()
    result_b = promote_attachment_to_document(
        db_session, vault, attachment_b, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    assert result_a.document.document_id == result_b.document.document_id
    assert db_session.query(Document).count() == 1

    links = db_session.scalars(select(CommunicationDocumentLink)).all()
    assert len(links) == 2
    assert {link.communication_id for link in links} == {
        communication_a.communication_id,
        communication_b.communication_id,
    }


def test_duplicate_promotion_writes_custody_event_on_existing_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    existing_source = tmp_path / "source-files" / "existing.pdf"
    existing_source.parent.mkdir(parents=True, exist_ok=True)
    import base64

    existing_source.write_bytes(base64.b64decode(b"JVBERi0xLjQK"))
    existing_document = ingest_document(
        db_session, vault, sample_case, source_file_path=existing_source,
        original_filename="2024 IEP.pdf", actor="test-user",
    )
    db_session.commit()

    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<dup3@x>"
    )
    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    events = db_session.scalars(
        select(DocumentCustodyEvent).where(DocumentCustodyEvent.document_id == existing_document.document_id)
    ).all()
    event_types = [e.event_type for e in events]
    assert "communication_attachment_linked" in event_types


# --- idempotency -------------------------------------------------------


def test_repeating_promotion_is_idempotent(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<idem1@x>"
    )

    first = promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    second = promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    assert second.document.document_id == first.document.document_id
    assert second.already_linked is True

    links = db_session.scalars(select(CommunicationDocumentLink)).all()
    assert len(links) == 1

    events = db_session.scalars(
        select(DocumentCustodyEvent).where(
            DocumentCustodyEvent.document_id == first.document.document_id,
            DocumentCustodyEvent.event_type == "communication_attachment_linked",
        )
    ).all()
    assert len(events) == 1


def test_repeating_duplicate_promotion_is_idempotent(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    existing_source = tmp_path / "source-files" / "existing.pdf"
    existing_source.parent.mkdir(parents=True, exist_ok=True)
    import base64

    existing_source.write_bytes(base64.b64decode(b"JVBERi0xLjQK"))
    ingest_document(
        db_session, vault, sample_case, source_file_path=existing_source,
        original_filename="2024 IEP.pdf", actor="test-user",
    )
    db_session.commit()

    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<idem2@x>"
    )

    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()
    promote_attachment_to_document(
        db_session, vault, attachment, document_type_id=None, source=None, notes=None,
        date_received=None, field_provenance=None, actor="test-user",
    )
    db_session.commit()

    assert db_session.query(Document).count() == 1
    assert db_session.query(CommunicationDocumentLink).count() == 1


# --- exclude / leave with email --------------------------------------------


def test_exclude_attachment_does_not_create_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<excl1@x>"
    )

    exclude_attachment(db_session, attachment, "test-user")
    db_session.commit()

    db_session.refresh(attachment)
    assert attachment.review_status == "excluded"
    assert attachment.resulting_document_id is None
    assert db_session.query(Document).count() == 0


def test_leave_with_email_does_not_create_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<leave1@x>"
    )

    leave_attachment_with_email(db_session, attachment, "test-user")
    db_session.commit()

    db_session.refresh(attachment)
    assert attachment.review_status == "left_with_email"
    assert attachment.resulting_document_id is None
    assert db_session.query(Document).count() == 0


def test_exclude_is_idempotent_no_duplicate_custody_events(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<excl2@x>"
    )

    exclude_attachment(db_session, attachment, "test-user")
    db_session.commit()
    exclude_attachment(db_session, attachment, "test-user")
    db_session.commit()

    events = db_session.scalars(
        select(CommunicationCustodyEvent).where(
            CommunicationCustodyEvent.communication_id == communication.communication_id,
            CommunicationCustodyEvent.event_type == "attachment_excluded",
        )
    ).all()
    assert len(events) == 1


# --- unsupported / missing file fails safely --------------------------------


def test_promotion_fails_safely_when_stored_file_missing(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication, attachment = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<missing1@x>"
    )
    full_path = vault.root / attachment.stored_path
    full_path.chmod(0o600)
    full_path.unlink()

    with pytest.raises(AttachmentNotPromotableError):
        promote_attachment_to_document(
            db_session, vault, attachment, document_type_id=None, source=None, notes=None,
            date_received=None, field_provenance=None, actor="test-user",
        )

    db_session.refresh(attachment)
    assert attachment.review_status == "pending"
    assert attachment.resulting_document_id is None
