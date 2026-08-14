"""End-to-end tests for the Step 10 bulk-import batch routes
(Communications Phase Step 10): creating a batch from browse/search
selections, the batch status page, and cancel/resume/retry-failed.

`enable_background_worker=False` in the test `Settings` fixture (see
tests/conftest.py), so creating a batch here never processes it
automatically -- tests call `process_next_batch()` directly, exactly
the same convention tests/test_api_ocr.py uses for `process_next_job()`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    Communication,
    CommunicationAttachment,
    CommunicationCustodyEvent,
    CommunicationImportBatch,
    CommunicationImportBatchItem,
)
from app.jobs.import_worker import process_next_batch
from tests.test_api_communications import _InMemoryKeyring, working_keyring  # noqa: F401
from tests.test_api_communications_imap import _connect_yahoo_account, _install_fake_transport
from tests.test_communications_imap_client import FakeImapTransport, _eml


def _create_case(client: TestClient, label: str = "Bulk Import Student") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _create_batch_via_browse_selection(
    client: TestClient, account_id: int, case_id: int, folder: str, uids: list[str]
) -> int:
    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": folder, "uid": uids, "case_id": str(case_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


# --- batch creation ------------------------------------------------------


def test_create_batch_from_selected_items(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)

    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1", "2"])

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "pending"
        assert batch.matched_count == 2
        assert batch.case_id == case_id
        assert batch.account_id == account_id
        items = db.scalars(
            select(CommunicationImportBatchItem).where(CommunicationImportBatchItem.batch_id == batch_id)
        ).all()
        assert {i.mailbox_uid for i in items} == {"1", "2"}


def test_create_batch_requires_a_case(client: TestClient, app: FastAPI, working_keyring):
    account_id = _connect_yahoo_account(client, app)
    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1"], "case_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_create_batch_requires_at_least_one_selected_uid(client: TestClient, app: FastAPI, working_keyring):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": [], "case_id": str(case_id)},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_create_batch_unknown_account_returns_404(client: TestClient):
    response = client.post(
        "/communications/999999/import-batches",
        data={"folder": "INBOX", "uid": ["1"], "case_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- batch status page -----------------------------------------------


def test_batch_status_page_shows_counters(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="amanda@district.example.org", subject="First Notice"),
            _eml(uid=2, sender="amanda@district.example.org", subject="Second Notice"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1", "2"])

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        process_next_batch(db, app.state.vault)

    response = client.get(f"/communications/import-batches/{batch_id}")
    assert response.status_code == 200
    assert "completed" in response.text
    assert "First Notice" not in response.text  # this page shows counts/items, not message content


def test_batch_status_page_unknown_id_returns_404(client: TestClient):
    response = client.get("/communications/import-batches/999999")
    assert response.status_code == 404


# --- cancel / resume / retry-failed routes --------------------------------


def test_cancel_route_stops_a_pending_batch(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1"])

    response = client.post(f"/communications/import-batches/{batch_id}/cancel", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "cancelled"


def test_resume_route_reactivates_a_failed_batch(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1"])

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        batch.status = "failed"
        db.commit()

    response = client.post(f"/communications/import-batches/{batch_id}/resume", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "pending"


def test_retry_failed_route_resets_failed_items(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes={"INBOX": []}
    )
    _install_fake_transport(monkeypatch, transport)

    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1"])

    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)  # uid 1 doesn't exist -- fails
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.failed_count == 1

    response = client.post(f"/communications/import-batches/{batch_id}/retry-failed", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.failed_count == 0
        assert batch.status == "pending"


# --- zero side effects from browsing alone --------------------------------


def test_browsing_without_start_import_produces_zero_communications(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    account_id = _connect_yahoo_account(client, app)
    _create_case(client)
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Never Selected")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})

    with app.state.session_factory() as db:
        assert db.query(Communication).count() == 0
        assert db.query(CommunicationAttachment).count() == 0
        assert db.query(CommunicationCustodyEvent).count() == 0
        assert db.query(CommunicationImportBatch).count() == 0


# --- full end-to-end: search -> select -> start import -> processed -------


def test_full_flow_select_and_import_produces_communications(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="amanda@district.example.org", subject="First Notice"),
            _eml(uid=2, sender="amanda@district.example.org", subject="Second Notice"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    # Search first, matching the real UI flow.
    search_response = client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})
    assert "First Notice" in search_response.text
    assert "Second Notice" in search_response.text

    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1", "2"])

    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "completed"
        assert batch.imported_count == 2
        communications = db.scalars(select(Communication)).all()
        assert len(communications) == 2
        assert all(c.import_method == "imap_sync" for c in communications)
        assert all(c.case_id == case_id for c in communications)


def test_disconnect_account_never_touches_batch_imported_communications(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    """docs/COMMUNICATIONS_PLAN.md §16: disconnecting only ever removes
    the credential/connection ability, never previously imported data --
    this must hold for Step 10's batch-imported messages exactly as it
    already does for manual .eml/.mbox imports."""
    account_id = _connect_yahoo_account(client, app)
    case_id = _create_case(client)
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Survives Disconnect")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    batch_id = _create_batch_via_browse_selection(client, account_id, case_id, "INBOX", ["1"])
    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)

    response = client.post(f"/communications/{account_id}/disconnect", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        communication = db.scalars(select(Communication)).one()
        assert communication.subject == "Survives Disconnect"
        batch = db.get(CommunicationImportBatch, batch_id)
        assert batch.status == "completed"
        assert batch.imported_count == 1


# --- unaffected existing behavior ------------------------------------------


def test_manual_eml_and_mbox_import_unaffected_by_batch_routes(client: TestClient, app: FastAPI):
    case_id = _create_case(client, "Regression Student")
    eml_bytes = (
        b"From: sender@example.org\r\n"
        b"To: parent@yahoo.com\r\n"
        b"Subject: Regression Check\r\n"
        b"Message-ID: <regression-batch-check@example.org>\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", eml_bytes, "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        communication = db.scalars(select(Communication)).one()
        assert communication.import_method == "manual_upload"
        assert communication.account_id is None
        assert communication.mailbox_folder is None
        assert communication.mailbox_uid is None
