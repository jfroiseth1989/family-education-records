"""Schema tests for the Communications Phase Step 1 tables.

See docs/COMMUNICATIONS_PLAN.md. No core module or UI exists yet (that's
Steps 2+) -- these tests exercise the ORM models directly to confirm the
schema itself (tables, foreign keys, the dedup constraint, multi-
provenance linking) is correct before any application logic is built on
top of it, matching the same approach test_timeline_schema.py took for
Phase 4 Step 1.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Case,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationCustodyEvent,
    CommunicationDocumentLink,
    CommunicationImportBatch,
    CommunicationImportBatchItem,
    CommunicationThread,
    Document,
)

_MARCH_7 = datetime.datetime(2022, 3, 7, tzinfo=datetime.timezone.utc)


def _account(db: Session, email_address: str = "parent@yahoo.com") -> CommunicationAccount:
    account = CommunicationAccount(
        provider="yahoo",
        email_address=email_address,
        auth_method="app_password",
        credential_ref="ferchronos-yahoo-1",
        created_by="test-user",
    )
    db.add(account)
    db.flush()
    return account


def _communication(
    db: Session,
    *,
    account: CommunicationAccount | None = None,
    case: Case | None = None,
    message_id_header: str | None = "<msg-1@yahoo.com>",
    sha256_hash: str = "a" * 64,
    import_method: str = "manual_upload",
) -> Communication:
    communication = Communication(
        account_id=account.account_id if account else None,
        case_id=case.case_id if case else None,
        subject="Re: IEP meeting",
        from_address="amanda.wagner@district.example.org",
        from_display_name="Amanda Wagner",
        sent_at=_MARCH_7,
        received_at=_MARCH_7,
        message_id_header=message_id_header,
        sha256_hash=sha256_hash,
        stored_path="cases/1/communications/aa/message.eml",
        file_size_bytes=1024,
        import_method=import_method,
        imported_by="test-user",
    )
    db.add(communication)
    db.flush()
    return communication


def _document(db: Session, case: Case) -> Document:
    document = Document(
        case_id=case.case_id,
        original_filename="2023 Annual IEP.pdf",
        stored_path="cases/1/documents/2023-annual-iep.pdf",
        sha256_hash="b" * 64,
        file_size_bytes=2048,
        ingested_by="test-user",
    )
    db.add(document)
    db.flush()
    return document


def test_communication_account_round_trips(db_session: Session):
    account = _account(db_session)
    db_session.commit()

    stored = db_session.get(CommunicationAccount, account.account_id)
    assert stored.provider == "yahoo"
    assert stored.auth_method == "app_password"
    assert stored.credential_ref == "ferchronos-yahoo-1"
    assert stored.status == "connected"
    assert stored.disconnected_at is None


def test_manual_upload_communication_has_no_account(db_session: Session, sample_case: Case):
    """Communications plan §1 decision 3: manual .eml upload must work
    with no connected mailbox at all.
    """
    communication = _communication(db_session, account=None, case=sample_case)
    db_session.commit()

    stored = db_session.get(Communication, communication.communication_id)
    assert stored.account_id is None
    assert stored.import_method == "manual_upload"
    assert stored.communication_type == "email"


def test_imap_synced_communication_links_its_account(db_session: Session, sample_case: Case):
    account = _account(db_session)
    communication = _communication(
        db_session, account=account, case=sample_case, import_method="imap_sync"
    )
    db_session.commit()

    stored = db_session.get(Communication, communication.communication_id)
    assert stored.account_id == account.account_id
    assert stored.account.email_address == "parent@yahoo.com"


def test_duplicate_message_id_within_account_is_rejected(db_session: Session, sample_case: Case):
    """Communications plan §7: primary dedup key is (account_id,
    message_id_header).
    """
    account = _account(db_session)
    _communication(db_session, account=account, case=sample_case, message_id_header="<dup@yahoo.com>")
    db_session.commit()

    with pytest.raises(IntegrityError):
        _communication(db_session, account=account, case=sample_case, message_id_header="<dup@yahoo.com>")
    db_session.rollback()


def test_multiple_communications_with_no_message_id_are_allowed(db_session: Session, sample_case: Case):
    """SQLite treats every NULL as distinct in a unique index -- messages
    with no Message-ID header (rare, but real) must never collide on that
    account, since the fallback dedup key is (account_id, sha256_hash),
    checked in application logic, not enforced here.
    """
    account = _account(db_session)
    _communication(db_session, account=account, case=sample_case, message_id_header=None, sha256_hash="c" * 64)
    _communication(db_session, account=account, case=sample_case, message_id_header=None, sha256_hash="d" * 64)
    db_session.commit()  # must not raise

    count = db_session.query(Communication).filter(Communication.account_id == account.account_id).count()
    assert count == 2


def test_same_message_id_allowed_across_different_accounts(db_session: Session, sample_case: Case):
    account_a = _account(db_session, email_address="a@yahoo.com")
    account_b = _account(db_session, email_address="b@yahoo.com")
    _communication(db_session, account=account_a, case=sample_case, message_id_header="<shared@yahoo.com>")
    _communication(db_session, account=account_b, case=sample_case, message_id_header="<shared@yahoo.com>")
    db_session.commit()  # must not raise


def test_communication_custody_event_ledger(db_session: Session, sample_case: Case):
    communication = _communication(db_session, case=sample_case)
    event = CommunicationCustodyEvent(
        communication_id=communication.communication_id,
        event_type="imported",
        actor="test-user",
        sha256_hash_at_event=communication.sha256_hash,
        file_size_bytes_at_event=communication.file_size_bytes,
        storage_location_at_event=communication.stored_path,
    )
    db_session.add(event)
    db_session.commit()

    stored = db_session.get(Communication, communication.communication_id)
    assert len(stored.custody_events) == 1
    assert stored.custody_events[0].event_type == "imported"
    assert stored.custody_events[0].sha256_hash_at_event == "a" * 64


def test_communication_thread_groups_messages(db_session: Session, sample_case: Case):
    thread = CommunicationThread(
        case_id=sample_case.case_id,
        subject_normalized="iep meeting",
        message_count=2,
        first_message_at=_MARCH_7,
        last_message_at=_MARCH_7,
    )
    db_session.add(thread)
    db_session.flush()

    first = _communication(db_session, case=sample_case, message_id_header="<t1@yahoo.com>")
    second = _communication(db_session, case=sample_case, message_id_header="<t2@yahoo.com>")
    first.thread_id = thread.thread_id
    second.thread_id = thread.thread_id
    db_session.commit()

    stored_thread = db_session.get(CommunicationThread, thread.thread_id)
    assert stored_thread.message_count == 2
    assert {c.communication_id for c in stored_thread.communications} == {
        first.communication_id,
        second.communication_id,
    }


def test_attachment_promotion_and_multi_email_provenance(db_session: Session, sample_case: Case):
    """Communications plan §8/§11: the same attachment arriving via two
    different emails must link to one Document, not create two.
    """
    document = _document(db_session, sample_case)

    email_one = _communication(db_session, case=sample_case, message_id_header="<one@yahoo.com>")
    attachment_one = CommunicationAttachment(
        communication_id=email_one.communication_id,
        filename="2023 Annual IEP.pdf",
        size_bytes=2048,
        sha256_hash="b" * 64,
        stored_path="cases/1/communications/attachments/b/2023-annual-iep.pdf",
        is_educational_record_candidate=True,
        review_status="added_to_documents",
        resulting_document_id=document.document_id,
    )
    db_session.add(attachment_one)
    db_session.flush()

    email_two = _communication(db_session, case=sample_case, message_id_header="<two@yahoo.com>")
    attachment_two = CommunicationAttachment(
        communication_id=email_two.communication_id,
        filename="2023 Annual IEP (resent).pdf",
        size_bytes=2048,
        sha256_hash="b" * 64,  # identical content -> same document
        stored_path="cases/1/communications/attachments/b/resent.pdf",
        is_educational_record_candidate=True,
        review_status="added_to_documents",
        resulting_document_id=document.document_id,
    )
    db_session.add(attachment_two)
    db_session.flush()

    db_session.add(
        CommunicationDocumentLink(
            document_id=document.document_id,
            communication_id=email_one.communication_id,
            communication_attachment_id=attachment_one.attachment_id,
        )
    )
    db_session.add(
        CommunicationDocumentLink(
            document_id=document.document_id,
            communication_id=email_two.communication_id,
            communication_attachment_id=attachment_two.attachment_id,
        )
    )
    db_session.commit()

    links = (
        db_session.query(CommunicationDocumentLink)
        .filter(CommunicationDocumentLink.document_id == document.document_id)
        .all()
    )
    assert len(links) == 2
    assert {link.communication_id for link in links} == {
        email_one.communication_id,
        email_two.communication_id,
    }
    # Still exactly one Document row -- no duplicate was created.
    assert db_session.query(Document).filter(Document.sha256_hash == "b" * 64).count() == 1


def test_communication_soft_delete_leaves_row_in_place(db_session: Session, sample_case: Case):
    communication = _communication(db_session, case=sample_case)
    db_session.commit()

    communication.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db_session.commit()

    stored = db_session.get(Communication, communication.communication_id)
    assert stored is not None
    assert stored.subject == "Re: IEP meeting"
    assert stored.deleted_at is not None


def test_import_batch_and_resumable_items(db_session: Session, sample_case: Case):
    """Communications plan §14: a batch's items track per-message
    progress so an interrupted import can resume from the first pending
    item without re-touching already-imported or already-skipped ones.
    """
    account = _account(db_session)
    batch = CommunicationImportBatch(
        account_id=account.account_id,
        case_id=sample_case.case_id,
        search_criteria={"date_from": "2022-01-01", "has_attachments": True},
        status="running",
        matched_count=3,
        created_by="test-user",
    )
    db_session.add(batch)
    db_session.flush()

    imported = _communication(db_session, account=account, case=sample_case, message_id_header="<i1@yahoo.com>")
    db_session.add_all(
        [
            CommunicationImportBatchItem(
                batch_id=batch.batch_id,
                mailbox_folder="INBOX",
                mailbox_uid="101",
                status="imported",
                communication_id=imported.communication_id,
            ),
            CommunicationImportBatchItem(
                batch_id=batch.batch_id, mailbox_folder="INBOX", mailbox_uid="102", status="skipped_duplicate"
            ),
            CommunicationImportBatchItem(
                batch_id=batch.batch_id, mailbox_folder="INBOX", mailbox_uid="103", status="pending"
            ),
        ]
    )
    db_session.commit()

    stored_batch = db_session.get(CommunicationImportBatch, batch.batch_id)
    assert len(stored_batch.items) == 3
    pending = [item for item in stored_batch.items if item.status == "pending"]
    assert len(pending) == 1
    assert pending[0].mailbox_uid == "103"


def test_import_batch_item_uid_unique_within_batch(db_session: Session, sample_case: Case):
    """A UID is only unique within its own folder (Communications Phase
    Step 9) -- the same UID in a *different* folder of the same batch
    must not collide, only a true repeat of (folder, uid) should."""
    account = _account(db_session)
    batch = CommunicationImportBatch(account_id=account.account_id, status="running", created_by="test-user")
    db_session.add(batch)
    db_session.flush()

    db_session.add(
        CommunicationImportBatchItem(batch_id=batch.batch_id, mailbox_folder="INBOX", mailbox_uid="1", status="pending")
    )
    db_session.commit()

    db_session.add(
        CommunicationImportBatchItem(batch_id=batch.batch_id, mailbox_folder="INBOX", mailbox_uid="1", status="pending")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Same UID, different folder -- must be accepted, not treated as a duplicate.
    db_session.add(
        CommunicationImportBatchItem(batch_id=batch.batch_id, mailbox_folder="Sent", mailbox_uid="1", status="pending")
    )
    db_session.commit()
