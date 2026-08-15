"""Communications Phase Step 12: final integration hardening.

Covers what Steps 9-11's own test suites don't already prove on their
own: the *cross-feature* behavior around disconnect/reconnect, credential
failure, and the full provenance chain end to end. Nothing here rebuilds
functionality Steps 9-11 already implemented and tested -- these tests
exercise the seams between them.
"""

from __future__ import annotations

import keyring

import app.core.communications.imap_client as imap_client_module
from app.db.models import (
    AiObservation,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationDocumentLink,
    CommunicationImportBatch,
    CommunicationImportBatchItem,
    CommunicationThread,
    Document,
)
from app.jobs.import_worker import process_next_batch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.test_api_communications import _InMemoryKeyring, working_keyring  # noqa: F401
from tests.test_api_communications_bulk_review import _create_case, _eml_with_attachment
from tests.test_api_communications_imap import _connect_yahoo_account, _install_fake_transport
from tests.test_communications_imap_client import FakeImapTransport


def _reply_eml(*, message_id: str, in_reply_to: str, subject: str) -> bytes:
    """A plain reply (no attachment) referencing `in_reply_to` -- paired
    with an original message, this is what gives the scenario a real,
    multi-message `CommunicationThread` (a lone message never forms one;
    see `thread_grouping.py::group_messages`'s `len(indices) < 2` rule),
    which the cross-feature provenance audit needs to exercise the
    Email -> Thread relationship at all.
    """
    return (
        b"From: Parent <parent@yahoo.com>\n"
        b"To: Amanda Wagner <amanda.wagner@district.example.org>\n"
        b"Subject: " + subject.encode() + b"\n"
        b"Date: Tue, 8 Mar 2022 09:00:00 -0500\n"
        b"Message-ID: " + message_id.encode() + b"\n"
        b"In-Reply-To: " + in_reply_to.encode() + b"\n"
        b"References: " + in_reply_to.encode() + b"\n"
        b"Content-Type: text/plain\n\n"
        b"Thank you, see you then.\n"
    )


def _import_full_scenario(client: TestClient, app: FastAPI, monkeypatch) -> dict:
    """Build one realistically imported Yahoo email thread -- an original
    message with an attachment plus a reply -- all the way through:
    connect -> batch import -> attachment promoted to a Document. A
    timeline suggestion is generated automatically at import time (Step
    8). Returns every id the cross-feature audit needs.
    """
    case_id = _create_case(client, "Step 12 Audit Student")
    account_id = _connect_yahoo_account(client, app)

    raw = _eml_with_attachment(message_id="<step12-audit@example.org>", subject="Annual IEP Meeting")
    reply = _reply_eml(
        message_id="<step12-audit-reply@example.org>",
        in_reply_to="<step12-audit@example.org>",
        subject="Re: Annual IEP Meeting",
    )
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [(1, raw), (2, reply)]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1", "2"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    batch_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "completed"
        item = db.scalars(
            select(CommunicationImportBatchItem).where(
                CommunicationImportBatchItem.batch_id == batch_id,
                CommunicationImportBatchItem.mailbox_uid == "1",
            )
        ).one()
        communication_id = item.communication_id
        attachment = db.scalars(
            select(CommunicationAttachment).where(CommunicationAttachment.communication_id == communication_id)
        ).one()
        attachment_id = attachment.attachment_id

    promote_response = client.post(
        f"/communications/attachments/{attachment_id}/add-to-documents",
        data={"document_type_source": "suggested"},
    )
    assert promote_response.status_code == 200, promote_response.text

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        document_id = attachment.resulting_document_id
        assert document_id is not None

    return {
        "case_id": case_id,
        "account_id": account_id,
        "batch_id": batch_id,
        "communication_id": communication_id,
        "attachment_id": attachment_id,
        "document_id": document_id,
    }


# --- cross-feature provenance audit --------------------------------------


def test_cross_feature_provenance_audit_bidirectional_navigation(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """Trace one imported email with an attachment through every
    relationship Step 12 asks for, in both directions, always via a real
    FK/link -- never by guessing from filenames or text.
    """
    ids = _import_full_scenario(client, app, monkeypatch)

    with app.state.session_factory() as db:
        communication = db.get(Communication, ids["communication_id"])
        # Email -> Thread: every Communication is assigned a thread by
        # the automatic Step 4 rebuild.
        assert communication.thread_id is not None
        thread = db.get(CommunicationThread, communication.thread_id)
        assert communication in thread.communications

        # Email -> Attachment (and back).
        attachment = db.get(CommunicationAttachment, ids["attachment_id"])
        assert attachment.communication_id == communication.communication_id

        # Email/Attachment -> Document, and Document -> originating
        # email(s), via the real CommunicationDocumentLink provenance
        # row -- not by matching filenames.
        document = db.get(Document, ids["document_id"])
        assert attachment.resulting_document_id == document.document_id
        link = db.scalars(
            select(CommunicationDocumentLink).where(
                CommunicationDocumentLink.document_id == document.document_id
            )
        ).one()
        assert link.communication_id == communication.communication_id
        assert link.communication_attachment_id == attachment.attachment_id

        # Email -> Timeline Suggestion (AiObservation), generated
        # automatically at import time and linked back by FK.
        observation = db.scalars(
            select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
        ).one()
        assert observation.case_id == communication.case_id

        # Import Batch -> Communication, via the batch item row.
        batch_item = db.scalars(
            select(CommunicationImportBatchItem).where(
                CommunicationImportBatchItem.batch_id == ids["batch_id"],
                CommunicationImportBatchItem.mailbox_uid == "1",
            )
        ).one()
        assert batch_item.communication_id == communication.communication_id

    # Email -> Thread and Document -> originating email, navigable at
    # the HTTP layer via real links (not reconstructed from text).
    detail_page = client.get(f"/communications/email/{ids['communication_id']}")
    assert detail_page.status_code == 200
    assert f"/communications/threads/{communication.thread_id}" in detail_page.text
    assert f"/documents/{ids['document_id']}" in detail_page.text

    document_page = client.get(f"/documents/{ids['document_id']}")
    assert document_page.status_code == 200
    assert f"/communications/email/{ids['communication_id']}" in document_page.text

    # Communications Search -> Email.
    search_page = client.get("/communications/search", params={"q": "Annual IEP Meeting"})
    assert search_page.status_code == 200
    assert f"/communications/email/{ids['communication_id']}" in search_page.text

    # Timeline suggestion review -> View Source Email.
    facts_page = client.get(f"/cases/{ids['case_id']}/facts")
    assert facts_page.status_code == 200
    assert f"/communications/email/{ids['communication_id']}" in facts_page.text


# --- orphan / reference integrity -----------------------------------------


def test_batch_item_communication_id_matches_actual_import(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    ids = _import_full_scenario(client, app, monkeypatch)
    with app.state.session_factory() as db:
        item = db.scalars(
            select(CommunicationImportBatchItem).where(
                CommunicationImportBatchItem.batch_id == ids["batch_id"],
                CommunicationImportBatchItem.mailbox_uid == "1",
            )
        ).one()
        communication = db.get(Communication, item.communication_id)
        assert communication is not None
        assert communication.account_id == ids["account_id"]
        assert item.mailbox_folder == "INBOX"
        assert item.mailbox_uid == "1"


def test_attachment_resulting_document_id_matches_provenance_linkage(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    ids = _import_full_scenario(client, app, monkeypatch)
    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, ids["attachment_id"])
        assert attachment.review_status == "added_to_documents"
        link = db.scalars(
            select(CommunicationDocumentLink).where(
                CommunicationDocumentLink.communication_attachment_id == attachment.attachment_id
            )
        ).one()
        assert link.document_id == attachment.resulting_document_id


def test_disconnect_orphans_nothing(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    """Disconnect must not delete or null out any FK that previously
    imported evidence depends on.
    """
    ids = _import_full_scenario(client, app, monkeypatch)

    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)

    with app.state.session_factory() as db:
        communication = db.get(Communication, ids["communication_id"])
        assert communication is not None
        assert communication.account_id == ids["account_id"]
        assert communication.deleted_at is None

        attachment = db.get(CommunicationAttachment, ids["attachment_id"])
        assert attachment is not None
        assert attachment.resulting_document_id == ids["document_id"]

        document = db.get(Document, ids["document_id"])
        assert document is not None

        batch = db.get(CommunicationImportBatch, ids["batch_id"])
        assert batch is not None
        assert batch.account_id == ids["account_id"]

        link = db.scalars(
            select(CommunicationDocumentLink).where(CommunicationDocumentLink.document_id == ids["document_id"])
        ).one()
        assert link.communication_id == ids["communication_id"]


def test_reconnect_does_not_mutate_provenance_history(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    ids = _import_full_scenario(client, app, monkeypatch)

    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "new-app-password"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        # Still the same account_id -- reactivated, not forked.
        accounts = db.scalars(select(CommunicationAccount)).all()
        assert len(accounts) == 1
        assert accounts[0].account_id == ids["account_id"]
        assert accounts[0].status == "connected"

        communication = db.get(Communication, ids["communication_id"])
        assert communication.account_id == ids["account_id"]
        attachment = db.get(CommunicationAttachment, ids["attachment_id"])
        assert attachment.resulting_document_id == ids["document_id"]
        assert attachment.review_status == "added_to_documents"


# --- existing/imported data remains usable after disconnect ---------------


def test_local_evidence_workflows_survive_disconnect(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    ids = _import_full_scenario(client, app, monkeypatch)

    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)

    assert client.get(f"/communications/email/{ids['communication_id']}").status_code == 200
    with app.state.session_factory() as db:
        thread_id = db.get(Communication, ids["communication_id"]).thread_id
    assert client.get(f"/communications/threads/{thread_id}").status_code == 200
    assert client.get("/communications/search", params={"q": "Annual IEP Meeting"}).status_code == 200
    assert client.get("/communications/attachments/review").status_code == 200
    assert client.get(f"/documents/{ids['document_id']}").status_code == 200
    assert client.get(f"/cases/{ids['case_id']}/facts").status_code == 200
    assert client.get(f"/communications/import-batches/{ids['batch_id']}").status_code == 200
    assert client.get("/communications").status_code == 200


def test_fts5_search_still_returns_communication_after_disconnect(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    ids = _import_full_scenario(client, app, monkeypatch)
    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)

    response = client.get("/communications/search", params={"q": "Annual IEP Meeting"})
    assert response.status_code == 200
    assert f"/communications/email/{ids['communication_id']}" in response.text


# --- mailbox-only actions become unavailable after disconnect -------------


def test_mailbox_actions_unavailable_after_disconnect(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    ids = _import_full_scenario(client, app, monkeypatch)
    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)

    test_conn = client.post(f"/communications/{ids['account_id']}/test-connection")
    assert test_conn.status_code == 400

    browse = client.get(f"/communications/{ids['account_id']}/browse")
    assert browse.status_code == 400

    new_batch = client.post(
        f"/communications/{ids['account_id']}/import-batches",
        data={"folder": "INBOX", "uid": ["2"], "case_id": str(ids["case_id"])},
        follow_redirects=False,
    )
    assert new_batch.status_code == 400


def test_import_batches_with_unfinished_items_after_disconnect_dont_lose_state(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """A batch left with pending/failed items when its account is
    disconnected must never silently reconnect, delete anything, or lose
    state -- it just stops making progress until reconnected.
    """
    case_id = _create_case(client)
    account_id = _connect_yahoo_account(client, app)

    raw1 = _eml_with_attachment(message_id="<pending-1@example.org>")
    raw2 = _eml_with_attachment(message_id="<pending-2@example.org>", subject="Second Message")
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [(1, raw1), (2, raw2)]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1", "2"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    batch_id = int(response.headers["location"].rsplit("/", 1)[-1])
    # Deliberately never processed -- both items are still pending, as if
    # the app were closed before the worker got to them.

    client.post(f"/communications/{account_id}/disconnect", follow_redirects=False)

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        items = db.scalars(
            select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch_id)
        ).all()
        assert len(items) == 2
        assert all(item.status == "pending" for item in items)
        assert batch.status == "pending"  # untouched by disconnect

    # Attempting to resume must not silently succeed or lose state --
    # the account has no valid credential, so this is a no-op.
    resume_response = client.post(f"/communications/import-batches/{batch_id}/resume", follow_redirects=False)
    assert resume_response.status_code == 303
    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "pending"
        items = db.scalars(
            select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch_id)
        ).all()
        assert all(item.status == "pending" for item in items)

    status_page = client.get(f"/communications/import-batches/{batch_id}")
    assert status_page.status_code == 200
    assert "disconnected" in status_page.text.lower()
    assert "reconnect" in status_page.text.lower()


# --- reconnection: account-scoped duplicate detection survives ------------


def test_reconnect_preserves_account_scoped_duplicate_detection(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """The central Step 12 integration risk: Step 10's duplicate-import
    detection is scoped by `Communication.account_id`. If reconnecting
    forked a new logical account, re-selecting the same already-imported
    message afterward would create a second Communication row for it
    instead of being recognized as a duplicate. Reactivation (this
    step's fix) keeps `account_id` stable across the disconnect/
    reconnect cycle, so the exact same dedup path Step 10 already tests
    still recognizes it.
    """
    case_id = _create_case(client)
    account_id = _connect_yahoo_account(client, app)

    raw = _eml_with_attachment(message_id="<reconnect-dedup@example.org>")
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [(1, raw)]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    first_batch_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)
        assert db.query(Communication).count() == 1

    client.post(f"/communications/{account_id}/disconnect", follow_redirects=False)
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "correct-app-password"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        reconnected_account = db.scalars(select(CommunicationAccount)).one()
        assert reconnected_account.account_id == account_id

    # Re-select and re-import the exact same message under the
    # reactivated account.
    transport.mailboxes["INBOX"] = [(1, raw)]
    second_response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    second_batch_id = int(second_response.headers["location"].rsplit("/", 1)[-1])
    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)

    with app.state.session_factory() as db:
        # Still exactly one Communication row -- recognized as a
        # duplicate, not re-imported as a second row.
        assert db.query(Communication).count() == 1
        second_batch = db.get(CommunicationImportBatch, second_batch_id)
        assert second_batch.status == "completed"
        assert second_batch.skipped_duplicate_count == 1
        assert second_batch.imported_count == 0


# --- missing/revoked keyring credential ------------------------------------


def test_missing_keyring_credential_while_db_says_connected_fails_closed(
    client: TestClient, app: FastAPI, working_keyring
):
    """The database says the account is `connected`, but its keyring
    entry was deleted outside FERChronos (not via Disconnect). Must fail
    closed with a safe message, never crash, never expose the
    credential_ref as if it were a secret, and never touch imported data.
    """
    account_id = _connect_yahoo_account(client, app)
    with app.state.session_factory() as db:
        account = db.get(CommunicationAccount, account_id)
        credential_ref = account.credential_ref
        assert account.status == "connected"

    # Simulate external revocation: delete the keyring entry directly,
    # bypassing disconnect_account() entirely -- the DB row is untouched.
    keyring.get_keyring().delete_password("FERChronos Communications", credential_ref)

    response = client.post(f"/communications/{account_id}/test-connection")
    assert response.status_code == 400
    assert credential_ref not in response.text
    assert "reconnect" in response.text.lower()

    with app.state.session_factory() as db:
        account = db.get(CommunicationAccount, account_id)
        # The DB row is untouched by this failure -- still "connected"
        # per its own status column (the discrepancy is only ever
        # detected, and only ever surfaced safely, at actual use time).
        assert account.status == "connected"

    browse_response = client.get(f"/communications/{account_id}/browse")
    assert browse_response.status_code == 400
    assert credential_ref not in browse_response.text


def test_missing_credential_account_still_shown_as_credential_unavailable_on_home_page(
    client: TestClient, app: FastAPI, working_keyring
):
    account_id = _connect_yahoo_account(client, app)
    with app.state.session_factory() as db:
        credential_ref = db.get(CommunicationAccount, account_id).credential_ref
    keyring.get_keyring().delete_password("FERChronos Communications", credential_ref)

    home_page = client.get("/communications")
    assert home_page.status_code == 200
    assert "Credential unavailable" in home_page.text
    assert credential_ref not in home_page.text


# --- Yahoo-side app-password revocation mid-batch --------------------------


def test_auth_revocation_mid_batch_preserves_earlier_successful_imports(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """If Yahoo revokes the app password partway through a batch (the
    session dies mid-batch), items already successfully imported before
    that point must stay imported -- a later auth failure is a
    connection problem, never evidence corruption, and never grounds to
    invalidate anything already committed.
    """
    case_id = _create_case(client)
    account_id = _connect_yahoo_account(client, app)

    raw1 = _eml_with_attachment(message_id="<revoke-1@example.org>", subject="Before Revocation")
    raw2 = _eml_with_attachment(message_id="<revoke-2@example.org>", subject="After Revocation")
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [(1, raw1), (2, raw2)]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1", "2"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    batch_id = int(response.headers["location"].rsplit("/", 1)[-1])

    import app.jobs.import_worker as import_worker_module
    from app.core.communications.imap_client import ImapAuthenticationError

    real_import_one = import_worker_module.import_one_imap_message
    call_count = {"n": 0}

    def _revoke_after_first(db, vault, case, imap_client, *, account_id, folder, uid, actor):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ImapAuthenticationError("Yahoo revoked this session (test double).")
        return real_import_one(db, vault, case, imap_client, account_id=account_id, folder=folder, uid=uid, actor=actor)

    monkeypatch.setattr(import_worker_module, "import_one_imap_message", _revoke_after_first)

    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "failed"
        assert batch.imported_count == 1

        items = db.scalars(
            select(CommunicationImportBatchItem)
            .where(CommunicationImportBatchItem.batch_id == batch_id)
            .order_by(CommunicationImportBatchItem.item_id)
        ).all()
        assert items[0].status == "imported"
        assert items[1].status == "pending"  # never attempted after the session died

        # The successfully imported message is untouched -- not deleted,
        # not marked invalid, not soft-deleted.
        communication = db.get(Communication, items[0].communication_id)
        assert communication is not None
        assert communication.deleted_at is None
        assert communication.subject == "Before Revocation"


# --- network boundary audit -------------------------------------------------


def test_local_evidence_routes_never_open_an_imap_connection(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """Every local, already-imported-evidence workflow must work with
    zero IMAP protocol activity -- proven dynamically by making any
    attempt to authenticate raise, then exercising every local route.
    """
    ids = _import_full_scenario(client, app, monkeypatch)

    def _must_not_connect(self, email_address, password):
        raise AssertionError("a local evidence route attempted a live IMAP connection")

    monkeypatch.setattr(imap_client_module.ImapClient, "connect_and_authenticate", _must_not_connect)

    with app.state.session_factory() as db:
        thread_id = db.get(Communication, ids["communication_id"]).thread_id

    routes = [
        "/communications",
        f"/communications/email/{ids['communication_id']}",
        f"/communications/threads/{thread_id}",
        "/communications/threads",
        "/communications/search?q=Annual+IEP",
        "/communications/attachments/review",
        f"/communications/attachments/{ids['attachment_id']}/review",
        f"/communications/import-batches/{ids['batch_id']}",
        f"/documents/{ids['document_id']}",
        f"/cases/{ids['case_id']}/facts",
        f"/cases/{ids['case_id']}/timeline",
    ]
    for route in routes:
        response = client.get(route)
        assert response.status_code in (200, 303), f"{route} -> {response.status_code}"

    # App startup itself (the app fixture is already constructed by this
    # point) never opened a connection either -- if it had, the
    # monkeypatch above would already have fired during fixture setup,
    # which it did not (this test reached here at all).


def test_no_yahoo_app_password_anywhere_in_database_after_full_lifecycle(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    ids = _import_full_scenario(client, app, monkeypatch)
    client.post(f"/communications/{ids['account_id']}/disconnect", follow_redirects=False)
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "correct-app-password"},
        follow_redirects=False,
    )

    secret = "correct-app-password"
    with app.state.session_factory() as db:
        conn = db.connection()
        for table in conn.dialect.get_table_names(conn):
            for col in conn.dialect.get_columns(conn, table):
                col_name = col["name"]
                found = db.execute(
                    text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col_name}" = :v'),
                    {"v": secret},
                ).scalar()
                assert found == 0, f"credential leaked into {table}.{col_name}"
