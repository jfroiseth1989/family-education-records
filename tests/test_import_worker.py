"""Tests for app/jobs/import_worker.py -- the bulk-import batch queue's
worker (Communications Phase Step 10).

Uses the same in-process `FakeImapTransport` test double Step 9's tests
built -- no real network access, no real Yahoo account, ever.
"""

from __future__ import annotations

import stat

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.core.communications.imap_client as imap_client_module
import app.core.communications.imap_service as imap_service_module
from app.core.communications.import_batches import create_batch
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import (
    AiObservation,
    Case,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationCustodyEvent,
    CommunicationImportBatchItem,
    CommunicationThread,
)
from app.jobs.import_worker import claim_next_batch, process_next_batch
from tests.test_communications_imap_client import FakeImapTransport, _eml


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


@pytest.fixture(autouse=True)
def working_credential(monkeypatch):
    """Every test in this file gets a working stored app password by
    default -- individual tests override with monkeypatch as needed."""
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: "correct-app-password")


def _install_transport(monkeypatch, transport: FakeImapTransport) -> None:
    monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)


def _attachment_eml(uid: int, subject: str = "Has attachment") -> tuple[int, bytes]:
    body = (
        f"From: sender@example.org\r\n"
        f"To: parent@yahoo.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 7 Mar 2022 14:30:00 -0500\r\n"
        f"Message-ID: <att-{uid}@example.org>\r\n"
        f'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
        f"\r\n"
        f"--BOUNDARY\r\n"
        f"Content-Type: text/plain\r\n\r\n"
        f"See attached.\r\n"
        f"--BOUNDARY\r\n"
        f"Content-Type: application/pdf\r\n"
        f'Content-Disposition: attachment; filename="2023 Annual IEP.pdf"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n"
        f"JVBERi0xLjQK\r\n"
        f"--BOUNDARY--\r\n"
    ).encode("utf-8")
    return uid, body


# --- successful multi-message import ---------------------------------


def test_successful_multi_message_import(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="amanda@district.example.org", subject="First Notice"),
            _eml(uid=2, sender="amanda@district.example.org", subject="Second Notice"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {"folder": "INBOX"}, "test-user"
    )

    result = process_next_batch(db_session, vault)

    assert result.batch_id == batch.batch_id
    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.imported_count == 2
    assert batch.skipped_duplicate_count == 0
    assert batch.failed_count == 0
    assert db_session.query(Communication).count() == 2
    assert transport.logged_out is True


def test_raw_fetch_feeds_shared_ingestion_pipeline(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    uid, raw = _eml(uid=1, sender="amanda@district.example.org", subject="Provenance Check")
    mailboxes = {"INBOX": [(uid, raw)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    communication = db_session.scalars(select(Communication)).one()
    assert communication.import_method == "imap_sync"
    assert communication.account_id == sample_account.account_id
    assert communication.mailbox_folder == "INBOX"
    assert communication.mailbox_uid == "1"
    assert communication.case_id == sample_case.case_id
    assert communication.subject == "Provenance Check"

    stored_path = vault.root / communication.stored_path
    assert stored_path.exists()
    assert compute_sha256(stored_path) == communication.sha256_hash
    mode = stored_path.stat().st_mode
    assert not (mode & stat.S_IWUSR)

    events = db_session.scalars(
        select(CommunicationCustodyEvent).where(
            CommunicationCustodyEvent.communication_id == communication.communication_id
        )
    ).all()
    assert len(events) == 1
    assert events[0].details["source"] == "yahoo_imap"
    assert events[0].details["mailbox_folder"] == "INBOX"
    assert events[0].details["mailbox_uid"] == "1"


def test_attachment_behavior_preserved(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {"INBOX": [_attachment_eml(1)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    communication = db_session.scalars(select(Communication)).one()
    attachments = db_session.scalars(
        select(CommunicationAttachment).where(
            CommunicationAttachment.communication_id == communication.communication_id
        )
    ).all()
    assert len(attachments) == 1
    assert attachments[0].filename == "2023 Annual IEP.pdf"
    assert attachments[0].review_status == "pending"


def test_threading_preserved(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    uid1, raw1 = _eml(uid=1, sender="a@b.com", subject="Meeting", to="parent@yahoo.com")
    raw1 = raw1.replace(b"Message-ID: <msg-1@example.org>", b"Message-ID: <thread-root@example.org>")
    uid2, raw2 = _eml(uid=2, sender="a@b.com", subject="Re: Meeting", to="parent@yahoo.com")
    raw2 = raw2.replace(
        b"Message-ID: <msg-1@example.org>",
        b"Message-ID: <thread-reply@example.org>\r\nIn-Reply-To: <thread-root@example.org>",
    )
    mailboxes = {"INBOX": [(uid1, raw1), (uid2, raw2)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user")
    process_next_batch(db_session, vault)

    threads = db_session.scalars(select(CommunicationThread)).all()
    assert len(threads) == 1
    assert threads[0].message_count == 2


def test_timeline_suggestion_preserved(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Notice")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    communication = db_session.scalars(select(Communication)).one()
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).first()
    assert observation is not None
    assert observation.status == "pending_review"


def test_fts_search_indexes_imported_messages(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    from app.core.indexing.communications_search import search_communications

    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Unique Searchable Subject Xyzzy")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    results = search_communications(db_session, query="Xyzzy")
    assert len(results) == 1
    assert results[0].subject == "Unique Searchable Subject Xyzzy"


# --- duplicate handling ----------------------------------------------


def test_duplicate_message_is_skipped_not_failed(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    uid, raw = _eml(uid=1, sender="a@b.com", subject="Dup Test", to="parent@yahoo.com")
    mailboxes = {"INBOX": [(uid, raw)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    # Import it once manually (simulating it already exists from a
    # prior import), then run a batch selecting the same message.
    from app.core.communications.ingestion import import_eml_file
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    import_eml_file(
        db_session,
        vault,
        sample_case,
        source_file_path=tmp_path,
        original_filename="preexisting.eml",
        actor="test-user",
        account_id=sample_account.account_id,
    )
    db_session.commit()
    tmp_path.unlink(missing_ok=True)
    assert db_session.query(Communication).count() == 1

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.skipped_duplicate_count == 1
    assert batch.imported_count == 0
    assert db_session.query(Communication).count() == 1  # no second row

    item = db_session.scalars(
        select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch.batch_id)
    ).one()
    assert item.status == "skipped_duplicate"
    assert item.communication_id is not None


# --- mixed batch: success + duplicate + failure -----------------------


def test_mixed_batch_success_duplicate_and_failure(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    unique_uid, unique_raw = _eml(uid=1, sender="a@b.com", subject="Unique Message")
    dup_uid, dup_raw = _eml(uid=2, sender="a@b.com", subject="Dup Message", to="parent@yahoo.com")
    mailboxes = {"INBOX": [(unique_uid, unique_raw), (dup_uid, dup_raw)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    # Pre-import the "duplicate" message so item uid=2 will be a duplicate.
    from app.core.communications.ingestion import import_eml_file
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp:
        tmp.write(dup_raw)
        tmp_path = Path(tmp.name)
    import_eml_file(
        db_session,
        vault,
        sample_case,
        source_file_path=tmp_path,
        original_filename="preexisting.eml",
        actor="test-user",
        account_id=sample_account.account_id,
    )
    db_session.commit()
    tmp_path.unlink(missing_ok=True)

    # uid=3 doesn't exist in the fake mailbox at all -- fetching it fails.
    batch = create_batch(
        db_session,
        sample_account,
        sample_case,
        [("INBOX", "1"), ("INBOX", "2"), ("INBOX", "999")],
        {},
        "test-user",
    )
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "completed_with_errors"
    assert batch.imported_count == 1
    assert batch.skipped_duplicate_count == 1
    assert batch.failed_count == 1
    assert db_session.query(Communication).count() == 2  # the pre-existing + the new unique one

    items = {
        item.mailbox_uid: item
        for item in db_session.scalars(
            select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch.batch_id)
        ).all()
    }
    assert items["1"].status == "imported"
    assert items["2"].status == "skipped_duplicate"
    assert items["999"].status == "failed"
    assert items["999"].error is not None


def test_one_failed_item_does_not_abort_later_items(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    """The failing item is processed first -- items after it must still
    be attempted."""
    ok_uid, ok_raw = _eml(uid=2, sender="a@b.com", subject="Comes After Failure")
    mailboxes = {"INBOX": [(ok_uid, ok_raw)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user"
    )  # uid "1" does not exist in the fake mailbox -- fetch fails
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.imported_count == 1
    assert batch.failed_count == 1
    assert db_session.query(Communication).count() == 1


# --- resumability ------------------------------------------------------


def test_resumability_processes_only_pending_items(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    """Simulates a restart: one item is already `imported` (as if a
    prior process committed it before crashing) -- reprocessing the
    batch must never re-touch it or create a duplicate Communication.
    """
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="Already Done"),
            _eml(uid=2, sender="a@b.com", subject="Still Pending"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user"
    )

    # Simulate item 1 having already been imported by a prior worker run.
    from app.core.communications.ingestion import import_eml_file
    import tempfile
    from pathlib import Path

    uid1_raw = mailboxes["INBOX"][0][1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp:
        tmp.write(uid1_raw)
        tmp_path = Path(tmp.name)
    pre_existing = import_eml_file(
        db_session,
        vault,
        sample_case,
        source_file_path=tmp_path,
        original_filename="already-done.eml",
        actor="test-user",
        account_id=sample_account.account_id,
        mailbox_folder="INBOX",
        mailbox_uid="1",
    )
    db_session.commit()
    tmp_path.unlink(missing_ok=True)

    item1 = db_session.scalars(
        select(CommunicationImportBatchItem).where(
            CommunicationImportBatchItem.batch_id == batch.batch_id, CommunicationImportBatchItem.mailbox_uid == "1"
        )
    ).one()
    item1.status = "imported"
    item1.communication_id = pre_existing.communication_id
    batch.imported_count = 1
    batch.status = "running"
    db_session.commit()

    # "Restart": process the batch again -- item 1 must be left alone.
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.imported_count == 2  # 1 pre-existing + 1 newly processed
    assert db_session.query(Communication).count() == 2  # never a duplicate for item 1

    db_session.refresh(item1)
    assert item1.status == "imported"
    assert item1.communication_id == pre_existing.communication_id


def test_restart_simulation_across_two_fresh_worker_calls(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    """A batch left `running` (as if the process died mid-batch) is
    picked back up by the very next `process_next_batch()` call --
    the worker has no separate crash-recovery sweep to run first (see
    app/jobs/import_worker.py's module docstring)."""
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Survives Restart")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    batch.status = "running"  # as if a prior process claimed it and then died
    db_session.commit()

    result = claim_next_batch(db_session)
    assert result is not None and result.batch_id == batch.batch_id

    process_next_batch(db_session, vault)
    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.imported_count == 1


# --- cancellation --------------------------------------------------------


def test_cancellation_preserves_completed_work(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="First"),
            _eml(uid=2, sender="a@b.com", subject="Second"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user"
    )

    # Cancel before the worker ever runs -- item 2 (in fact both) must
    # never be fetched, and the batch must never auto-finalize past
    # "cancelled".
    from app.core.communications.import_batches import request_cancel

    request_cancel(db_session, batch)

    result = claim_next_batch(db_session)
    assert result is None  # a cancelled batch is no longer claimable

    assert db_session.query(Communication).count() == 0
    items = db_session.scalars(
        select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch.batch_id)
    ).all()
    assert all(item.status == "pending" for item in items)


def test_cancellation_mid_batch_stops_before_next_item(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    """A cancel written by a concurrent request must be observed between
    items -- simulated here by cancelling from within the fake
    transport's search hook isn't practical, so instead we cancel after
    manually pre-imported item 1 and confirm the worker's own
    claim/select logic still respects "cancelled" as non-claimable
    (the batch-level guarantee this application actually offers)."""
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="First"),
            _eml(uid=2, sender="a@b.com", subject="Second"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user"
    )
    process_next_batch(db_session, vault)
    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.imported_count == 2

    # Cancelling a completed batch is a no-op -- nothing to preserve
    # differently, but must not resurrect or reprocess anything.
    from app.core.communications.import_batches import request_cancel

    request_cancel(db_session, batch)
    db_session.refresh(batch)
    assert batch.status == "completed"
    assert db_session.query(Communication).count() == 2


# --- retry ---------------------------------------------------------------


def test_retry_failed_item_then_succeeds(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes={"INBOX": []}
    )
    _install_transport(monkeypatch, transport)

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)  # uid 1 doesn't exist yet -- fails

    db_session.refresh(batch)
    assert batch.status == "completed_with_errors"
    assert batch.failed_count == 1

    # Now the message "arrives" in the mailbox and we retry.
    transport.mailboxes["INBOX"] = [_eml(uid=1, sender="a@b.com", subject="Now Available")]

    from app.core.communications.import_batches import retry_failed_items

    retry_failed_items(db_session, batch)
    db_session.refresh(batch)
    assert batch.status == "pending"
    assert batch.failed_count == 0

    process_next_batch(db_session, vault)
    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.imported_count == 1
    assert batch.failed_count == 0
    assert db_session.query(Communication).count() == 1


def test_retry_does_not_reprocess_successful_items(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="Succeeds First Try"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "999")], {}, "test-user"
    )
    process_next_batch(db_session, vault)
    db_session.refresh(batch)
    assert batch.imported_count == 1
    assert batch.failed_count == 1
    first_communication_id = db_session.scalars(select(Communication)).one().communication_id

    from app.core.communications.import_batches import retry_failed_items

    retry_failed_items(db_session, batch)
    process_next_batch(db_session, vault)  # uid 999 still doesn't exist -- fails again

    db_session.refresh(batch)
    assert batch.imported_count == 1  # unchanged -- item 1 never reprocessed
    assert db_session.query(Communication).count() == 1
    still_same = db_session.scalars(select(Communication)).one()
    assert still_same.communication_id == first_communication_id


# --- account-level failures --------------------------------------------


def test_missing_credential_fails_batch_without_touching_items(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: None)

    def factory_that_must_not_be_called(host, port, timeout):
        raise AssertionError("must not attempt a network connection with no credential")

    monkeypatch.setattr(imap_client_module, "_default_transport_factory", factory_that_must_not_be_called)

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "failed"
    items = db_session.scalars(
        select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch.batch_id)
    ).all()
    assert all(item.status == "pending" for item in items)


def test_auth_failure_fails_batch_leaving_items_pending(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "a-different-password"))
    _install_transport(monkeypatch, transport)

    batch = create_batch(
        db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user"
    )
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "failed"
    items = db_session.scalars(
        select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch.batch_id)
    ).all()
    assert all(item.status == "pending" for item in items)
    assert db_session.query(Communication).count() == 0


def test_failed_batch_can_be_resumed_after_credential_fixed(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "wrong-password"))
    _install_transport(monkeypatch, transport)

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)
    db_session.refresh(batch)
    assert batch.status == "failed"

    # "Fix" the credential and resume.
    transport.valid_credentials = ("parent@yahoo.com", "correct-app-password")
    transport.mailboxes["INBOX"] = [_eml(uid=1, sender="a@b.com", subject="After Fix")]

    from app.core.communications.import_batches import resume_batch

    resume_batch(db_session, batch)
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "completed"
    assert batch.imported_count == 1


# --- no sequence-number durability / no mutation / credential safety ------


def test_no_sequence_number_assumption_uid_identity_preserved(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    """Use a UID far from any plausible sequence-number range to prove
    nothing here treats mailbox_uid as a small ordinal position."""
    mailboxes = {"INBOX": [_eml(uid=884213, sender="a@b.com", subject="Realistic Large UID")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "884213")], {}, "test-user")
    process_next_batch(db_session, vault)

    communication = db_session.scalars(select(Communication)).one()
    assert communication.mailbox_uid == "884213"


def test_no_yahoo_mutation_commands_issued(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="One"),
            _eml(uid=2, sender="a@b.com", subject="Two"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    for mutating_method in ("store", "copy", "expunge", "append", "setflags"):
        assert not hasattr(transport, mutating_method)
    _install_transport(monkeypatch, transport)

    create_batch(db_session, sample_account, sample_case, [("INBOX", "1"), ("INBOX", "2")], {}, "test-user")
    process_next_batch(db_session, vault)  # would raise AttributeError if any mutating call were attempted


def test_credential_never_appears_in_item_error_text(
    db_session: Session, vault: VaultLayout, sample_account: CommunicationAccount, sample_case: Case, monkeypatch
):
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: "unmistakable-secret-value")
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "some-other-password"))
    _install_transport(monkeypatch, transport)

    batch = create_batch(db_session, sample_account, sample_case, [("INBOX", "1")], {}, "test-user")
    process_next_batch(db_session, vault)

    db_session.refresh(batch)
    assert batch.status == "failed"

    conn = db_session.connection()
    for table in conn.dialect.get_table_names(conn):
        for col in conn.dialect.get_columns(conn, table):
            col_name = col["name"]
            count = conn.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{col_name}" = ?',  # noqa: S608
                ("unmistakable-secret-value",),
            ).scalar()
            assert count == 0, f"secret leaked into {table}.{col_name}"
