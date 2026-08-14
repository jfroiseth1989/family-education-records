"""Tests for app/core/communications/bulk_attachment_review.py -- bulk
orchestration over the Step 5 single-attachment review logic
(Communications Phase Step 11).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.bulk_attachment_review import (
    AttachmentReviewFilters,
    bulk_add_to_documents,
    bulk_exclude,
    bulk_leave_with_email,
    list_recognized_pending_attachments,
    list_review_attachments,
    preview_bulk_promotion,
)
from app.core.communications.ingestion import import_eml_file
from app.core.communications.promotion import promote_attachment_to_document
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import (
    Case,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationImportBatch,
    CommunicationImportBatchItem,
    Document,
    DocumentType,
)


def _eml_with_attachment(
    *,
    message_id: str,
    subject: str = "IEP Meeting Notice",
    filename: str = "2024 Annual IEP.pdf",
    sender: str = "amanda.wagner@district.example.org",
    body_b64: str = "JVBERi0xLjQK",
) -> bytes:
    return (
        b"From: Amanda Wagner <" + sender.encode() + b">\n"
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
        + body_b64.encode() + b"\n"
        b"--BOUNDARY--\n"
    )


def _import_with_attachment(
    db: Session, vault: VaultLayout, case: Case, tmp_path: Path, *, message_id: str, name: str = "notice.eml", **kwargs
) -> tuple[Communication, CommunicationAttachment]:
    source = tmp_path / "source-files" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_eml_with_attachment(message_id=message_id, **kwargs))
    communication = import_eml_file(
        db, vault, case, source_file_path=source, original_filename=name, actor="test-user"
    )
    db.commit()
    attachment = db.scalars(
        select(CommunicationAttachment).where(CommunicationAttachment.communication_id == communication.communication_id)
    ).one()
    return communication, attachment


def _link_to_batch(db: Session, communication: Communication, *, batch: CommunicationImportBatch | None = None) -> CommunicationImportBatch:
    if batch is None:
        account = CommunicationAccount(
            provider="yahoo",
            email_address="parent@yahoo.com",
            auth_method="app_password",
            credential_ref="yahoo-test-ref",
            created_by="test-user",
        )
        db.add(account)
        db.flush()
        batch = CommunicationImportBatch(
            account_id=account.account_id,
            case_id=communication.case_id,
            status="completed",
            matched_count=1,
            imported_count=1,
            created_by="test-user",
        )
        db.add(batch)
        db.flush()
    db.add(
        CommunicationImportBatchItem(
            batch_id=batch.batch_id,
            mailbox_folder="INBOX",
            mailbox_uid=str(communication.communication_id),
            status="imported",
            communication_id=communication.communication_id,
        )
    )
    db.commit()
    return batch


# --- listing / filtering ---------------------------------------------------


def test_list_pending_attachments_default(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, attachment = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>")
    rows = list_review_attachments(db_session, AttachmentReviewFilters())
    assert len(rows) == 1
    assert rows[0].attachment.attachment_id == attachment.attachment_id


def test_pending_filter_excludes_reviewed(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    from app.core.communications.promotion import exclude_attachment

    _, attachment = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>")
    exclude_attachment(db_session, attachment, "test-user")
    db_session.commit()

    rows = list_review_attachments(db_session, AttachmentReviewFilters())
    assert rows == []


def test_filter_by_batch_id(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    comm1, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    comm2, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml")
    batch = _link_to_batch(db_session, comm1)

    rows = list_review_attachments(db_session, AttachmentReviewFilters(batch_id=batch.batch_id))
    assert {r.attachment.attachment_id for r in rows} == {att1.attachment_id}


def test_filter_by_case_id(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    other_case = Case(label="Other Student")
    db_session.add(other_case)
    db_session.commit()

    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    _import_with_attachment(db_session, vault, other_case, tmp_path, message_id="<b@example.org>", name="b.eml")

    rows = list_review_attachments(db_session, AttachmentReviewFilters(case_id=sample_case.case_id))
    assert {r.attachment.attachment_id for r in rows} == {att1.attachment_id}


def test_filter_by_document_type_id(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, iep_att = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml",
        filename="2024 Annual IEP.pdf",
    )
    _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml",
        filename="random_file.pdf", subject="Just an update",
    )
    iep_type_id = db_session.scalars(select(DocumentType.type_id).where(DocumentType.name == "IEP")).one()

    rows = list_review_attachments(db_session, AttachmentReviewFilters(document_type_id=iep_type_id))
    assert {r.attachment.attachment_id for r in rows} == {iep_att.attachment_id}


def test_filter_recognized_only(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, recognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml",
        filename="2024 Annual IEP.pdf",
    )
    _, unrecognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml",
        filename="random_file.pdf", subject="Just an update",
    )

    rows = list_review_attachments(db_session, AttachmentReviewFilters(candidate_filter="recognized"))
    assert {r.attachment.attachment_id for r in rows} == {recognized.attachment_id}

    rows_un = list_review_attachments(db_session, AttachmentReviewFilters(candidate_filter="unrecognized"))
    assert {r.attachment.attachment_id for r in rows_un} == {unrecognized.attachment_id}


def test_filter_duplicates_only(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA"
    )
    # Promote att1 to create a real Document with the same bytes.
    result = promote_attachment_to_document(
        db_session, vault, att1, document_type_id=None, source=None, notes=None, date_received=None,
        field_provenance=None, actor="test-user",
    )
    db_session.commit()
    assert result.created_new_document is True

    # A second, different email with the SAME attachment bytes -- a
    # genuine duplicate-of-an-existing-Document candidate.
    _, att2 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="AAAA"
    )
    # A third, unrelated attachment -- not a duplicate.
    _, att3 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<c@example.org>", name="c.eml", body_b64="BBBB"
    )

    rows = list_review_attachments(db_session, AttachmentReviewFilters(candidate_filter="duplicates"))
    ids = {r.attachment.attachment_id for r in rows}
    assert att2.attachment_id in ids
    assert att3.attachment_id not in ids
    # att1 itself is no longer pending (it was promoted), so the default
    # pending filter already excludes it regardless.


def test_filter_by_sender_and_subject_and_date(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    # _eml_with_attachment always uses the fixed display name "Amanda
    # Wagner", so the sender filter must be exercised against the part
    # of the From header that actually varies (the address) to be a
    # meaningful test.
    _, att1 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml",
        sender="amanda@district.example.org", subject="IEP Meeting Notice",
    )
    _, att2 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml",
        sender="other@otherdistrict.example.org", subject="Cafeteria Menu",
    )

    rows_sender = list_review_attachments(db_session, AttachmentReviewFilters(sender="amanda@district"))
    assert {r.attachment.attachment_id for r in rows_sender} == {att1.attachment_id}

    rows_subject = list_review_attachments(db_session, AttachmentReviewFilters(subject="Cafeteria"))
    assert {r.attachment.attachment_id for r in rows_subject} == {att2.attachment_id}


# --- Add all recognized safety --------------------------------------------


def test_add_all_recognized_excludes_unambiguous_only(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, recognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml",
        filename="2024 Annual IEP.pdf",
    )
    _, unrecognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml",
        filename="random_file.pdf", subject="Just an update",
    )
    candidates = list_recognized_pending_attachments(db_session)
    assert {a.attachment_id for a in candidates} == {recognized.attachment_id}


def test_add_all_recognized_excludes_already_reviewed(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    from app.core.communications.promotion import exclude_attachment

    _, att1 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml",
        filename="2024 Annual IEP.pdf",
    )
    exclude_attachment(db_session, att1, "test-user")
    db_session.commit()

    candidates = list_recognized_pending_attachments(db_session)
    assert candidates == []


def test_add_all_recognized_scoped_by_batch(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    comm1, att1 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", filename="2024 Annual IEP.pdf"
    )
    comm2, att2 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", filename="Progress Report.pdf",
        subject="Progress",
    )
    batch = _link_to_batch(db_session, comm1)

    candidates = list_recognized_pending_attachments(db_session, batch_id=batch.batch_id)
    assert {a.attachment_id for a in candidates} == {att1.attachment_id}


def test_preview_bulk_promotion_counts(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA"
    )
    result = promote_attachment_to_document(
        db_session, vault, att1, document_type_id=None, source=None, notes=None, date_received=None,
        field_provenance=None, actor="test-user",
    )
    db_session.commit()

    # Same bytes as att1 -- will link, not create.
    _, att2 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="AAAA"
    )
    # New bytes -- will create.
    _, att3 = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<c@example.org>", name="c.eml", body_b64="BBBB"
    )

    preview = preview_bulk_promotion(db_session, [att2, att3])
    assert preview.will_link_count == 1
    assert preview.will_add_count == 1
    assert preview.not_processable_count == 0


# --- bulk add ---------------------------------------------------------


def test_bulk_add_selected_success(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA")
    _, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="BBBB")

    summary = bulk_add_to_documents(db_session, vault, [att1, att2], "test-user")
    assert summary.added_count == 2
    assert summary.failed_count == 0
    assert db_session.query(Document).count() == 2

    db_session.refresh(att1)
    db_session.refresh(att2)
    assert att1.review_status == "added_to_documents"
    assert att2.review_status == "added_to_documents"


def test_bulk_add_links_duplicate_not_second_document(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA")
    _, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="AAAA")

    summary = bulk_add_to_documents(db_session, vault, [att1, att2], "test-user")
    assert summary.added_count == 1
    assert summary.linked_count == 1
    assert db_session.query(Document).count() == 1

    doc_id = db_session.scalars(select(Document.document_id)).one()
    db_session.refresh(att1)
    db_session.refresh(att2)
    assert att1.resulting_document_id == doc_id
    assert att2.resulting_document_id == doc_id


def test_multiple_duplicate_emails_still_produce_one_document(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    atts = []
    for i in range(4):
        _, att = _import_with_attachment(
            db_session, vault, sample_case, tmp_path, message_id=f"<msg{i}@example.org>", name=f"m{i}.eml", body_b64="AAAA"
        )
        atts.append(att)

    summary = bulk_add_to_documents(db_session, vault, atts, "test-user")
    assert summary.added_count == 1
    assert summary.linked_count == 3
    assert db_session.query(Document).count() == 1

    from app.db.models import CommunicationDocumentLink

    links = db_session.query(CommunicationDocumentLink).all()
    assert len(links) == 4  # provenance preserved for every originating email


def test_bulk_add_all_recognized_end_to_end(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, recognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", filename="2024 Annual IEP.pdf"
    )
    _, unrecognized = _import_with_attachment(
        db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", filename="random_file.pdf",
        subject="Just an update",
    )

    candidates = list_recognized_pending_attachments(db_session)
    summary = bulk_add_to_documents(db_session, vault, candidates, "test-user")
    assert summary.added_count == 1

    db_session.refresh(recognized)
    db_session.refresh(unrecognized)
    assert recognized.review_status == "added_to_documents"
    assert unrecognized.review_status == "pending"  # untouched -- never auto-promoted


# --- exclude / leave with email -----------------------------------------


def test_bulk_exclude_selected(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    _, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml")

    summary = bulk_exclude(db_session, [att1, att2], "test-user")
    assert summary.failed_count == 0
    db_session.refresh(att1)
    db_session.refresh(att2)
    assert att1.review_status == "excluded"
    assert att2.review_status == "excluded"
    assert db_session.query(Document).count() == 0


def test_bulk_leave_with_email_selected(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")

    summary = bulk_leave_with_email(db_session, [att1], "test-user")
    assert summary.failed_count == 0
    db_session.refresh(att1)
    assert att1.review_status == "left_with_email"


# --- mixed / failure isolation / idempotency -----------------------------


def test_mixed_bulk_success_duplicate_and_failure(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, existing_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<x@example.org>", name="x.eml", body_b64="AAAA")
    promote_attachment_to_document(
        db_session, vault, existing_att, document_type_id=None, source=None, notes=None, date_received=None,
        field_provenance=None, actor="test-user",
    )
    db_session.commit()

    _, dup_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA")
    _, new_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="BBBB")

    # Simulate a real failure: the file backing the attachment vanishes
    # from the vault before the bulk action runs.
    _, broken_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<c@example.org>", name="c.eml", body_b64="CCCC")
    (vault.root / broken_att.stored_path).unlink()

    summary = bulk_add_to_documents(db_session, vault, [dup_att, new_att, broken_att], "test-user")
    assert summary.linked_count == 1
    assert summary.added_count == 1
    assert summary.failed_count == 1

    db_session.refresh(dup_att)
    db_session.refresh(new_att)
    db_session.refresh(broken_att)
    assert dup_att.review_status == "added_to_documents"
    assert new_att.review_status == "added_to_documents"
    assert broken_att.review_status == "pending"  # failure never partially applied


def test_failure_does_not_undo_earlier_successes(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, ok_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA")
    _, broken_att = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="BBBB")
    (vault.root / broken_att.stored_path).unlink()

    summary = bulk_add_to_documents(db_session, vault, [ok_att, broken_att], "test-user")
    assert summary.added_count == 1
    assert summary.failed_count == 1
    assert db_session.query(Document).count() == 1  # the successful one persisted despite the later failure


def test_repeated_add_selected_is_idempotent(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")

    first = bulk_add_to_documents(db_session, vault, [att1], "test-user")
    assert first.added_count == 1

    second = bulk_add_to_documents(db_session, vault, [att1], "test-user")
    assert second.already_reviewed_count == 1
    assert second.added_count == 0
    assert db_session.query(Document).count() == 1


# --- metadata preservation -------------------------------------------------


def test_user_edited_metadata_preserved_when_bulk_reprocessed(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    """An attachment already promoted with human-entered metadata (via
    the single-item flow) must never be re-touched by a later bulk
    action -- it's simply skipped as already_reviewed."""
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    result = promote_attachment_to_document(
        db_session, vault, att1, document_type_id=None, source="Custom Source", notes="My own custom notes",
        date_received=None, field_provenance={"source": "manual", "notes": "manual"}, actor="test-user",
    )
    db_session.commit()
    document_id = result.document.document_id

    summary = bulk_add_to_documents(db_session, vault, [att1], "test-user")
    assert summary.already_reviewed_count == 1

    document = db_session.get(Document, document_id)
    assert document.source == "Custom Source"
    assert document.notes == "My own custom notes"


def test_existing_document_metadata_never_overwritten_by_bulk_duplicate_link(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml", body_b64="AAAA")
    result = promote_attachment_to_document(
        db_session, vault, att1, document_type_id=None, source="Original Source", notes="Original notes",
        date_received=None, field_provenance={"source": "manual", "notes": "manual"}, actor="test-user",
    )
    db_session.commit()
    document_id = result.document.document_id

    _, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml", body_b64="AAAA")
    bulk_add_to_documents(db_session, vault, [att2], "test-user")

    document = db_session.get(Document, document_id)
    assert document.source == "Original Source"
    assert document.notes == "Original notes"


def test_reviewed_attachments_disappear_from_pending_view(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    _, att2 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<b@example.org>", name="b.eml")

    bulk_add_to_documents(db_session, vault, [att1], "test-user")

    rows = list_review_attachments(db_session, AttachmentReviewFilters())
    assert {r.attachment.attachment_id for r in rows} == {att2.attachment_id}


def test_bulk_action_never_modifies_attachment_hash_or_file(db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path):
    _, att1 = _import_with_attachment(db_session, vault, sample_case, tmp_path, message_id="<a@example.org>", name="a.eml")
    original_hash = att1.sha256_hash
    stored_path = vault.root / att1.stored_path
    original_bytes = stored_path.read_bytes()

    bulk_add_to_documents(db_session, vault, [att1], "test-user")

    db_session.refresh(att1)
    assert att1.sha256_hash == original_hash
    assert stored_path.read_bytes() == original_bytes
    assert compute_sha256(stored_path) == original_hash


def test_no_imap_dependency_in_bulk_review_module():
    """Static guard: no Yahoo/IMAP network call is even possible from
    this module -- it must not import anything from the imap_client/
    imap_service modules at all."""
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "communications" / "bulk_attachment_review.py"
    )
    source = module_path.read_text()
    assert "imap_client" not in source
    assert "imap_service" not in source
