"""End-to-end tests for the Step 9 read-only Yahoo IMAP browsing routes
(Communications Phase Step 9): `POST .../test-connection` and
`GET .../browse`.

Every test here proves the same two things at the HTTP layer that
tests/test_communications_imap_client.py proves at the unit layer: (1)
no real network access is ever used (a fake transport stands in), and
(2) browsing/searching a mailbox never writes a `Communication`,
`CommunicationAttachment`, vault file, or custody event -- Step 9 is
inspection only.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.core.communications.imap_client as imap_client_module
from app.db.models import (
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationCustodyEvent,
)
from tests.test_api_communications import _InMemoryKeyring, working_keyring  # noqa: F401
from tests.test_communications_imap_client import FakeImapTransport, _eml


def _connect_yahoo_account(client: TestClient, app: FastAPI) -> int:
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "correct-app-password"},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        account = db.scalars(select(CommunicationAccount)).one()
        return account.account_id


def _install_fake_transport(monkeypatch, transport: FakeImapTransport) -> None:
    monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)


# --- test-connection ---------------------------------------------------


def test_test_connection_success_shows_folder_count(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        folder_defs=[("INBOX", "\\HasNoChildren", "/"), ("Sent", "\\HasNoChildren \\Sent", "/")],
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.post(f"/communications/{account_id}/test-connection", follow_redirects=False)
    assert response.status_code == 200
    assert "Connected successfully" in response.text
    assert "2 folder" in response.text
    assert transport.logged_out is True


def test_test_connection_auth_failure_shows_safe_message(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "some-other-password"))
    _install_fake_transport(monkeypatch, transport)

    response = client.post(f"/communications/{account_id}/test-connection", follow_redirects=False)
    assert response.status_code == 401
    assert "rejected the app password" in response.text
    assert "correct-app-password" not in response.text


def test_test_connection_network_failure_shows_safe_message(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), fail_mode="network")
    _install_fake_transport(monkeypatch, transport)

    response = client.post(f"/communications/{account_id}/test-connection", follow_redirects=False)
    assert response.status_code == 503
    assert "Could not reach" in response.text


def test_test_connection_failure_never_disconnects_or_deletes_the_account(
    client: TestClient, app: FastAPI, working_keyring, monkeypatch
):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "some-other-password"))
    _install_fake_transport(monkeypatch, transport)

    client.post(f"/communications/{account_id}/test-connection", follow_redirects=False)

    with app.state.session_factory() as db:
        account = db.get(CommunicationAccount, account_id)
        assert account is not None
        assert account.status == "connected"


def test_test_connection_unknown_account_returns_404(client: TestClient):
    response = client.post("/communications/999999/test-connection", follow_redirects=False)
    assert response.status_code == 404


# --- browse / search -----------------------------------------------------


def test_browse_lists_folders_dynamically(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        folder_defs=[
            ("INBOX", "\\HasNoChildren", "/"),
            ("Bulk Mail", "\\HasNoChildren \\Junk", "/"),
        ],
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse")
    assert response.status_code == 200
    assert "Bulk Mail" in response.text
    assert "No email has been imported yet" in response.text


def test_browse_search_returns_bounded_previews(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="amanda@district.example.org", subject="IEP Meeting Notice"),
            _eml(uid=2, sender="other@example.org", subject="Cafeteria Menu"),
        ]
    }
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX", "sender": "amanda"})
    assert response.status_code == 200
    assert "IEP Meeting Notice" in response.text
    assert "Cafeteria Menu" not in response.text
    assert "showing 1 of 1 match" in response.text


def test_browse_search_zero_results(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [_eml(uid=1, sender="a@b.com")]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX", "subject": "nonexistent"})
    assert response.status_code == 200
    assert "No matches found" in response.text


def test_browse_auth_failure_shows_safe_error(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "wrong"))
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse")
    assert response.status_code == 401
    assert "rejected the app password" in response.text


def test_browse_unknown_account_returns_404(client: TestClient):
    response = client.get("/communications/999999/browse")
    assert response.status_code == 404


# --- zero side effects: the core Step 9 guarantee -------------------------


def test_browse_and_search_create_zero_communications_rows(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    mailboxes = {"INBOX": [_eml(uid=i, sender="a@b.com", subject=f"Msg {i}") for i in range(1, 6)]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})

    with app.state.session_factory() as db:
        assert db.query(Communication).count() == 0
        assert db.query(CommunicationAttachment).count() == 0
        assert db.query(CommunicationCustodyEvent).count() == 0


def test_browse_and_search_write_zero_vault_files(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Msg")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    _install_fake_transport(monkeypatch, transport)

    vault_root = app.state.vault.root
    files_before = sorted(p for p in vault_root.rglob("*") if p.is_file())

    client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})

    files_after = sorted(p for p in vault_root.rglob("*") if p.is_file())
    assert files_before == files_after


def test_test_connection_credential_never_appears_in_response(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"))
    _install_fake_transport(monkeypatch, transport)

    response = client.post(f"/communications/{account_id}/test-connection", follow_redirects=False)
    assert "correct-app-password" not in response.text


def test_browse_credential_never_appears_in_response_or_url(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [_eml(uid=1, sender="a@b.com")]},
    )
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX", "sender": "a@b.com"})
    assert "correct-app-password" not in response.text
    assert "correct-app-password" not in str(response.url)


def test_browse_never_issues_a_mutating_transport_call(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com")]}
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "correct-app-password"), mailboxes=mailboxes)
    for mutating_method in ("store", "copy", "expunge", "append"):
        assert not hasattr(transport, mutating_method)
    _install_fake_transport(monkeypatch, transport)

    response = client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})
    assert response.status_code == 200


def test_browse_selects_mailbox_readonly(client: TestClient, app: FastAPI, working_keyring, monkeypatch):
    account_id = _connect_yahoo_account(client, app)
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [_eml(uid=1, sender="a@b.com")]},
    )
    _install_fake_transport(monkeypatch, transport)

    client.get(f"/communications/{account_id}/browse", params={"folder": "INBOX"})
    assert transport.selected_readonly is True


# --- existing .eml/.mbox import behavior is unaffected ---------------------


def test_manual_eml_upload_still_works_alongside_step_9_routes(client: TestClient, app: FastAPI):
    case_response = client.post("/cases", data={"label": "Import Regression Student"}, follow_redirects=False)
    case_id = int(case_response.headers["location"].rsplit("/", 1)[-1])

    eml_bytes = (
        b"From: sender@example.org\r\n"
        b"To: parent@yahoo.com\r\n"
        b"Subject: Regression Check\r\n"
        b"Message-ID: <regression-check@example.org>\r\n"
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
    assert response.headers["location"].startswith("/communications/email/")

    with app.state.session_factory() as db:
        communication = db.scalars(select(Communication)).one()
        assert communication.import_method == "manual_upload"
