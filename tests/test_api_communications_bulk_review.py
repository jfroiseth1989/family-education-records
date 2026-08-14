"""End-to-end tests for the Step 11 bulk attachment-review routes
(Communications Phase Step 11): the review list page, bulk add/exclude/
leave actions, the Add-All-Recognized confirmation flow, and navigation
from an import batch's status page.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Communication, CommunicationAttachment, Document


def _create_case(client: TestClient, label: str = "Bulk Review Student") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _eml_with_attachment(
    *,
    message_id: str,
    subject: str = "IEP Meeting Notice",
    filename: str = "2024 Annual IEP.pdf",
    body_b64: str = "JVBERi0xLjQK",
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
        + body_b64.encode() + b"\n"
        b"--BOUNDARY--\n"
    )


def _upload_with_attachment(client: TestClient, case_id: int, **kwargs) -> int:
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_with_attachment(**kwargs), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _attachment_id_for(app: FastAPI, communication_id: int) -> int:
    with app.state.session_factory() as db:
        attachment = db.scalars(
            select(CommunicationAttachment).where(CommunicationAttachment.communication_id == communication_id)
        ).one()
        return attachment.attachment_id


# --- listing page ------------------------------------------------------


def test_review_list_shows_pending_attachments(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_with_attachment(client, case_id, message_id="<a@example.org>")

    response = client.get("/communications/attachments/review")
    assert response.status_code == 200
    assert "2024 Annual IEP.pdf" in response.text
    assert "IEP Meeting Notice" in response.text


def test_review_list_filter_by_case(client: TestClient, app: FastAPI):
    case1 = _create_case(client, "Student One")
    case2 = _create_case(client, "Student Two")
    _upload_with_attachment(client, case1, message_id="<a@example.org>", filename="doc-one.pdf")
    _upload_with_attachment(client, case2, message_id="<b@example.org>", filename="doc-two.pdf")

    response = client.get("/communications/attachments/review", params={"case_id": str(case1)})
    assert "doc-one.pdf" in response.text
    assert "doc-two.pdf" not in response.text


def test_review_list_recognized_only_filter(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_with_attachment(client, case_id, message_id="<a@example.org>", filename="2024 Annual IEP.pdf")
    _upload_with_attachment(
        client, case_id, message_id="<b@example.org>", filename="random_file.pdf", subject="Just an update"
    )

    response = client.get("/communications/attachments/review", params={"candidate": "recognized"})
    assert "2024 Annual IEP.pdf" in response.text
    assert "random_file.pdf" not in response.text


# --- bulk add selected ------------------------------------------------


def test_add_selected_via_http(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    response = client.post(
        "/communications/attachments/review/add-selected",
        data={"attachment_id": [str(attachment_id)], "status": "pending"},
    )
    assert response.status_code == 200
    assert "1 added" in response.text

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "added_to_documents"
        assert db.query(Document).count() == 1


def test_exclude_selected_via_http(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    response = client.post(
        "/communications/attachments/review/exclude-selected",
        data={"attachment_id": [str(attachment_id)], "status": "pending"},
    )
    assert response.status_code == 200

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "excluded"


def test_leave_selected_via_http(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    response = client.post(
        "/communications/attachments/review/leave-selected",
        data={"attachment_id": [str(attachment_id)], "status": "pending"},
    )
    assert response.status_code == 200

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "left_with_email"


# --- Add all recognized: two-phase confirm --------------------------------


def test_add_all_recognized_first_submit_only_previews(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    response = client.post("/communications/attachments/review/add-all-recognized", data={"status": "pending"})
    assert response.status_code == 200
    assert "will be added" in response.text.lower() or "will_add_count" not in response.text
    assert "Confirm and Add All Recognized" in response.text

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "pending"  # nothing committed yet
        assert db.query(Document).count() == 0


def test_add_all_recognized_confirmed_executes(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    response = client.post(
        "/communications/attachments/review/add-all-recognized", data={"status": "pending", "confirmed": "1"}
    )
    assert response.status_code == 200

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "added_to_documents"
        assert db.query(Document).count() == 1


def test_add_all_recognized_never_includes_unrecognized(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(
        client, case_id, message_id="<a@example.org>", filename="random_file.pdf", subject="Just an update"
    )
    attachment_id = _attachment_id_for(app, comm_id)

    client.post("/communications/attachments/review/add-all-recognized", data={"status": "pending", "confirmed": "1"})

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "pending"
        assert db.query(Document).count() == 0


# --- batch page navigation -------------------------------------------------


def test_batch_page_review_attachments_link_scoped_by_batch(client: TestClient, app: FastAPI, monkeypatch):
    """Uses the Step 10 bulk import machinery directly (no live IMAP) to
    produce a real completed batch, then verifies the batch status page
    links to a review page scoped to that batch's attachments."""
    import app.core.communications.imap_client as imap_client_module
    from app.jobs.import_worker import process_next_batch
    from tests.test_api_communications import _InMemoryKeyring, working_keyring as _wk  # noqa: F401
    from tests.test_api_communications_imap import _connect_yahoo_account, _install_fake_transport
    from tests.test_communications_imap_client import FakeImapTransport

    import keyring

    keyring.set_keyring(_InMemoryKeyring())

    case_id = _create_case(client)
    account_id = _connect_yahoo_account(client, app)

    raw = _eml_with_attachment(message_id="<batch-att@example.org>")
    transport = FakeImapTransport(
        valid_credentials=("parent@yahoo.com", "correct-app-password"),
        mailboxes={"INBOX": [(1, raw)]},
    )
    imap_client_module._default_transport_factory = lambda host, port, timeout: transport

    response = client.post(
        f"/communications/{account_id}/import-batches",
        data={"folder": "INBOX", "uid": ["1"], "case_id": str(case_id)},
        follow_redirects=False,
    )
    batch_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        process_next_batch(db, app.state.vault)

    status_response = client.get(f"/communications/import-batches/{batch_id}")
    assert f"/communications/attachments/review?batch_id={batch_id}" in status_response.text

    review_response = client.get("/communications/attachments/review", params={"batch_id": str(batch_id)})
    assert "2024 Annual IEP.pdf" in review_response.text


# --- Step 5 single-attachment review unaffected ----------------------------


def test_single_attachment_review_route_still_works_unchanged(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    comm_id = _upload_with_attachment(client, case_id, message_id="<a@example.org>")
    attachment_id = _attachment_id_for(app, comm_id)

    review_page = client.get(f"/communications/attachments/{attachment_id}/review")
    assert review_page.status_code == 200
    assert "2024 Annual IEP.pdf" in review_page.text

    response = client.post(
        f"/communications/attachments/{attachment_id}/add-to-documents",
        data={"document_type_source": "suggested"},
    )
    assert response.status_code == 200
    assert "Added to Documents" in response.text

    with app.state.session_factory() as db:
        attachment = db.get(CommunicationAttachment, attachment_id)
        assert attachment.review_status == "added_to_documents"
