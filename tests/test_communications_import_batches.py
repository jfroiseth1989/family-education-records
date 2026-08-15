"""Tests for app/core/communications/import_batches.py -- bulk-import
batch lifecycle bookkeeping (Communications Phase Step 10).

Pure database tests -- no IMAP, no fake transport, since this module
never opens a network connection itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.communications.import_batches import (
    BatchValidationError,
    count_remaining,
    create_batch,
    request_cancel,
    resume_batch,
    retry_failed_items,
)
from app.db.models import Case, CommunicationAccount, CommunicationImportBatchItem


@pytest.fixture
def sample_account(db_session: Session) -> CommunicationAccount:
    account = CommunicationAccount(
        provider="yahoo",
        email_address="parent@yahoo.com",
        auth_method="app_password",
        credential_ref="yahoo-test-ref",
        created_by="test-user",
    )
    db_session.add(account)
    db_session.commit()
    return account


def test_create_batch_persists_full_work_queue_up_front(
    db_session: Session, sample_account: CommunicationAccount, sample_case: Case
):
    batch = create_batch(
        db_session,
        sample_account,
        sample_case,
        items=[("INBOX", "1"), ("INBOX", "2"), ("INBOX", "3")],
        search_criteria={"folder": "INBOX"},
        actor="test-user",
    )
    assert batch.batch_id is not None
    assert batch.status == "pending"
    assert batch.matched_count == 3
    assert batch.account_id == sample_account.account_id
    assert batch.case_id == sample_case.case_id

    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .all()
    )
    assert len(items) == 3
    assert all(item.status == "pending" for item in items)
    assert {(i.mailbox_folder, i.mailbox_uid) for i in items} == {
        ("INBOX", "1"),
        ("INBOX", "2"),
        ("INBOX", "3"),
    }


def test_create_batch_requires_a_case(db_session: Session, sample_account: CommunicationAccount):
    with pytest.raises(BatchValidationError):
        create_batch(
            db_session, sample_account, None, items=[("INBOX", "1")], search_criteria={}, actor="test-user"
        )
    assert db_session.query(CommunicationImportBatchItem).count() == 0


def test_create_batch_requires_at_least_one_item(
    db_session: Session, sample_account: CommunicationAccount, sample_case: Case
):
    with pytest.raises(BatchValidationError):
        create_batch(db_session, sample_account, sample_case, items=[], search_criteria={}, actor="test-user")


def test_create_batch_deduplicates_selected_items(
    db_session: Session, sample_account: CommunicationAccount, sample_case: Case
):
    batch = create_batch(
        db_session,
        sample_account,
        sample_case,
        items=[("INBOX", "1"), ("INBOX", "1"), ("INBOX", "2")],
        search_criteria={},
        actor="test-user",
    )
    assert batch.matched_count == 2
    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .all()
    )
    assert len(items) == 2


def test_create_batch_same_uid_different_folders_are_distinct_items(
    db_session: Session, sample_account: CommunicationAccount, sample_case: Case
):
    """The same numeric UID in two different folders must never collapse
    into one item -- a UID is only unique within its own folder."""
    batch = create_batch(
        db_session,
        sample_account,
        sample_case,
        items=[("INBOX", "1"), ("Sent", "1")],
        search_criteria={},
        actor="test-user",
    )
    assert batch.matched_count == 2


def test_request_cancel_from_pending(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    request_cancel(db_session, batch)
    assert batch.status == "cancelled"


def test_request_cancel_from_running(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "running"
    db_session.commit()
    request_cancel(db_session, batch)
    assert batch.status == "cancelled"


def test_request_cancel_no_op_on_completed(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "completed"
    db_session.commit()
    request_cancel(db_session, batch)
    assert batch.status == "completed"


def test_resume_batch_from_failed(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "failed"
    db_session.commit()
    resume_batch(db_session, batch)
    assert batch.status == "pending"


def test_resume_batch_no_op_on_completed(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "completed"
    db_session.commit()
    resume_batch(db_session, batch)
    assert batch.status == "completed"


def test_resume_batch_never_touches_items(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user")
    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .all()
    )
    items[0].status = "imported"
    items[1].status = "pending"
    batch.status = "failed"
    db_session.commit()

    resume_batch(db_session, batch)

    db_session.refresh(items[0])
    db_session.refresh(items[1])
    assert items[0].status == "imported"
    assert items[1].status == "pending"


def test_retry_failed_items_resets_only_failed(db_session: Session, sample_account, sample_case):
    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2"), ("INBOX", "3")], {}, "test-user"
    )
    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .order_by(CommunicationImportBatchItem.item_id)
        .all()
    )
    items[0].status = "imported"
    items[1].status = "failed"
    items[1].error = "some safe error"
    items[2].status = "failed"
    items[2].error = "another safe error"
    batch.failed_count = 2
    batch.status = "completed_with_errors"
    db_session.commit()

    reset_count = retry_failed_items(db_session, batch)

    assert reset_count == 2
    db_session.refresh(items[0])
    db_session.refresh(items[1])
    db_session.refresh(items[2])
    assert items[0].status == "imported"  # untouched
    assert items[1].status == "pending"
    assert items[1].error is None
    assert items[2].status == "pending"
    assert items[2].error is None
    assert batch.failed_count == 0
    assert batch.status == "pending"


def test_retry_failed_items_no_op_when_none_failed(db_session: Session, sample_account, sample_case):
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    reset_count = retry_failed_items(db_session, batch)
    assert reset_count == 0
    assert batch.status == "pending"


def test_resume_batch_no_op_when_account_disconnected(db_session: Session, sample_account, sample_case):
    """Step 12: resuming a failed batch must never re-arm it for the
    worker while its account has no valid connected credential -- doing
    so would only cost a pointless running-then-failed cycle. The batch
    stays exactly `failed` until the account is reconnected.
    """
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "failed"
    sample_account.status = "disconnected"
    db_session.commit()

    resume_batch(db_session, batch)

    assert batch.status == "failed"


def test_retry_failed_items_no_op_when_account_disconnected(db_session: Session, sample_account, sample_case):
    """Step 12: same reasoning as resume_batch -- resetting failed items
    to pending is pointless (and misleading) while the account has no
    valid connected credential.
    """
    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user")
    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .all()
    )
    items[0].status = "failed"
    items[0].error = "some safe error"
    batch.failed_count = 1
    batch.status = "completed_with_errors"
    sample_account.status = "disconnected"
    db_session.commit()

    reset_count = retry_failed_items(db_session, batch)

    assert reset_count == 0
    db_session.refresh(items[0])
    assert items[0].status == "failed"
    assert batch.status == "completed_with_errors"


def test_count_remaining(db_session: Session, sample_account, sample_case):
    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2"), ("INBOX", "3")], {}, "test-user"
    )
    items = (
        db_session.query(CommunicationImportBatchItem)
        .filter(CommunicationImportBatchItem.batch_id == batch.batch_id)
        .all()
    )
    items[0].status = "imported"
    db_session.commit()
    assert count_remaining(db_session, batch) == 2
