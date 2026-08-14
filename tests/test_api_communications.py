"""End-to-end tests for the Communications routes (Communications Phase
Step 2): the accounts/home page, Connect Yahoo, and Disconnect.

CSRF-specific coverage lives in test_csrf_enforcement.py (the project's
established per-area convention); this file covers auth gating, the
connect/disconnect lifecycle through the real HTTP layer, and -- the
most important property of this step -- that the submitted app password
never appears in any response body or ends up stored anywhere in the
database.
"""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import CommunicationAccount


class _InMemoryKeyring(keyring.backend.KeyringBackend):
    """Minimal genuinely-working fake backend so the connect-success path
    can be exercised at the HTTP layer -- see
    tests/test_communications_credentials.py's module docstring for why
    this is scoped to a fixture rather than relied on ambiently.
    """

    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        del self._store[(service, username)]


class _NoKeyringBackend(keyring.backend.KeyringBackend):
    """Explicitly simulates "no secret store available" -- see
    tests/test_communications_credentials.py's module docstring for why
    this is set deliberately per-test rather than relied upon as an
    ambient environment default (a `working_keyring`-using test earlier
    in this same process registers a competing, higher-priority fake
    backend class, which can otherwise get auto-selected by `keyring`'s
    own memoized backend detection).
    """

    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend available (test double)")

    def set_password(self, service, username, password):
        raise keyring.errors.NoKeyringError("no backend available (test double)")

    def delete_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend available (test double)")


@pytest.fixture
def working_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(_InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(original)


@pytest.fixture
def no_keyring_available():
    original = keyring.get_keyring()
    keyring.set_keyring(_NoKeyringBackend())
    try:
        yield
    finally:
        keyring.set_keyring(original)


# --- auth gating -------------------------------------------------------


def test_communications_home_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- home page rendering ------------------------------------------------


def test_communications_home_shows_empty_state_with_no_accounts(client: TestClient):
    response = client.get("/communications")
    assert response.status_code == 200
    assert "No mailboxes connected yet." in response.text
    assert "Connect Yahoo" in response.text


def test_communications_home_explains_app_password_not_real_password(client: TestClient):
    """docs/COMMUNICATIONS_PLAN.md: the UI must make explicit that this
    is a Yahoo app password, distinct from the real account password.
    """
    response = client.get("/communications")
    assert "app password" in response.text.lower()
    assert "never asks for" in response.text.lower() or "never ask" in response.text.lower()
    assert "Account Security" in response.text


# --- connect: success -----------------------------------------------------


def test_connect_yahoo_success_creates_account_and_redirects(
    client: TestClient, app: FastAPI, working_keyring
):
    response = client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "my-app-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/communications"

    with app.state.session_factory() as db:
        account = db.scalars(select(CommunicationAccount)).one()
        assert account.email_address == "parent@yahoo.com"
        assert account.provider == "yahoo"
        assert account.auth_method == "app_password"
        assert account.status == "connected"


def test_connect_yahoo_success_never_syncs_or_creates_communications(
    client: TestClient, app: FastAPI, working_keyring
):
    """Connecting must only ever save the credential/account -- no IMAP
    call, no sync, no imported message -- per docs/COMMUNICATIONS_PLAN.md
    Step 2/§16.
    """
    from app.db.models import Communication

    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "my-app-password-123"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        assert db.query(Communication).count() == 0


def test_connect_yahoo_response_never_echoes_the_password(client: TestClient, working_keyring):
    response = client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "unmistakable-secret-value"},
        follow_redirects=True,
    )
    assert "unmistakable-secret-value" not in response.text


def test_connect_yahoo_never_writes_the_password_anywhere_in_the_database(
    client: TestClient, app: FastAPI, working_keyring
):
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "unmistakable-secret-value"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        # Raw-SQL scan of every column of every table in the whole
        # database -- not just the table this feature added -- for the
        # submitted secret. Broad by design: the requirement is "never
        # in SQLite," not "never in this one table."
        found_in = []
        conn = db.connection()
        for table in conn.dialect.get_table_names(conn):
            for col in conn.dialect.get_columns(conn, table):
                col_name = col["name"]
                count = conn.exec_driver_sql(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{col_name}" = ?',  # noqa: S608
                    ("unmistakable-secret-value",),
                ).scalar()
                if count:
                    found_in.append((table, col_name))

        assert found_in == [], f"Secret value found in database columns: {found_in}"


# --- connect: validation and failure ---------------------------------------


def test_connect_yahoo_missing_email_returns_400(client: TestClient, working_keyring):
    response = client.post(
        "/communications/yahoo/connect",
        data={"email_address": "", "app_password": "something"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_connect_yahoo_missing_password_returns_400(client: TestClient, working_keyring):
    response = client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_connect_yahoo_fails_closed_without_keyring_backend(
    client: TestClient, app: FastAPI, no_keyring_available
):
    """Must not fall back to any other storage, and must not create an
    account row, when no secure credential store is reachable.
    """
    response = client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "my-app-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert "secure credential store" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.query(CommunicationAccount).count() == 0


# --- disconnect -----------------------------------------------------------


def test_disconnect_unknown_account_returns_404(client: TestClient):
    response = client.post("/communications/999999/disconnect", follow_redirects=False)
    assert response.status_code == 404


def test_disconnect_marks_status_and_home_page_reflects_it(
    client: TestClient, app: FastAPI, working_keyring
):
    client.post(
        "/communications/yahoo/connect",
        data={"email_address": "parent@yahoo.com", "app_password": "my-app-password-123"},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        account_id = db.scalars(select(CommunicationAccount)).one().account_id

    response = client.post(f"/communications/{account_id}/disconnect", follow_redirects=False)
    assert response.status_code == 303

    home = client.get("/communications")
    assert "disconnected" in home.text.lower()
    # No "Disconnect" button should remain for an already-disconnected account.
    assert home.text.count('action="/communications/%d/disconnect"' % account_id) == 0


# --- manual .eml upload (Communications Phase Step 3) -----------------------


def _create_case(client: TestClient, label: str = "Manual Upload Student") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _eml_bytes(message_id: str = "<msg-1@example.org>", subject: str = "IEP Meeting Notice") -> bytes:
    return (
        f"From: Amanda Wagner <amanda.wagner@district.example.org>\n"
        f"To: parent@yahoo.com\n"
        f"Subject: {subject}\n"
        f"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        f"Message-ID: {message_id}\n"
        f"\n"
        f"Please see the attached notice.\n"
    ).encode("utf-8")


def test_upload_email_requires_no_connected_account(client: TestClient, app: FastAPI):
    """docs/COMMUNICATIONS_PLAN.md §1 decision 3: manual .eml upload must
    work with zero connected mailboxes -- this test never touches
    keyring or CommunicationAccount at all.
    """
    from app.db.models import Communication

    case_id = _create_case(client)
    with app.state.session_factory() as db:
        assert db.query(CommunicationAccount).count() == 0

    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_bytes(), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications/email/")

    with app.state.session_factory() as db:
        communication = db.scalars(select(Communication)).one()
        assert communication.account_id is None
        assert communication.case_id == case_id
        assert communication.import_method == "manual_upload"


def test_upload_email_missing_case_returns_400(client: TestClient):
    response = client.post(
        "/communications/upload",
        data={"case_id": ""},
        files={"file": ("notice.eml", _eml_bytes(), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_email_unknown_case_returns_400(client: TestClient):
    response = client.post(
        "/communications/upload",
        data={"case_id": "999999"},
        files={"file": ("notice.eml", _eml_bytes(), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_email_rejects_non_eml_extension(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.pdf", b"not an eml", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_duplicate_email_returns_409_and_no_second_row(client: TestClient, app: FastAPI):
    from app.db.models import Communication

    case_id = _create_case(client)
    client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_bytes(message_id="<dup@example.org>"), "message/rfc822")},
        follow_redirects=False,
    )

    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice-again.eml", _eml_bytes(message_id="<dup@example.org>"), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "already exists" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.query(Communication).count() == 1


def test_upload_email_original_bytes_preserved(client: TestClient, app: FastAPI):
    from app.db.models import Communication

    case_id = _create_case(client)
    content = _eml_bytes(subject="Exact bytes check")
    upload_response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", content, "message/rfc822")},
        follow_redirects=False,
    )
    communication_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        communication = db.get(Communication, communication_id)
        stored_path = app.state.vault.root / communication.stored_path
        assert stored_path.read_bytes() == content


# --- manual .mbox archive upload (Communications Phase Step 8) --------------


def _mbox_bytes(messages: list[bytes]) -> bytes:
    parts = []
    for msg in messages:
        parts.append(b"From sender@example.org Mon Mar  7 14:30:00 2022\n" + msg + b"\n")
    return b"".join(parts)


def test_upload_mbox_imports_each_message_and_redirects_home(client: TestClient, app: FastAPI):
    from app.db.models import Communication

    case_id = _create_case(client)
    archive = _mbox_bytes(
        [
            _eml_bytes(message_id="<one@example.org>", subject="First Notice"),
            _eml_bytes(message_id="<two@example.org>", subject="Second Notice"),
        ]
    )

    response = client.post(
        "/communications/upload-mbox",
        data={"case_id": str(case_id)},
        files={"file": ("archive.mbox", archive, "application/mbox")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "2 message(s) imported" in response.text

    with app.state.session_factory() as db:
        communications = db.scalars(select(Communication)).all()
        assert len(communications) == 2
        assert all(c.import_method == "mbox_import" for c in communications)
        assert all(c.case_id == case_id for c in communications)


def test_upload_mbox_missing_case_returns_400(client: TestClient):
    response = client.post(
        "/communications/upload-mbox",
        data={"case_id": ""},
        files={"file": ("archive.mbox", _mbox_bytes([_eml_bytes()]), "application/mbox")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_mbox_rejects_non_mbox_extension(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        "/communications/upload-mbox",
        data={"case_id": str(case_id)},
        files={"file": ("archive.eml", _mbox_bytes([_eml_bytes()]), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_mbox_reports_duplicates_without_failing(client: TestClient, app: FastAPI):
    from app.db.models import Communication

    case_id = _create_case(client)
    archive = _mbox_bytes([_eml_bytes(message_id="<dup@example.org>")])

    client.post(
        "/communications/upload-mbox",
        data={"case_id": str(case_id)},
        files={"file": ("archive.mbox", archive, "application/mbox")},
        follow_redirects=False,
    )
    response = client.post(
        "/communications/upload-mbox",
        data={"case_id": str(case_id)},
        files={"file": ("archive.mbox", archive, "application/mbox")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "1 duplicate(s) skipped" in response.text

    with app.state.session_factory() as db:
        assert db.query(Communication).count() == 1


def test_communication_detail_page_shows_subject_body_and_attachments(client: TestClient):
    case_id = _create_case(client)
    upload_response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_bytes(subject="Detail Page Subject"), "message/rfc822")},
        follow_redirects=False,
    )
    detail_url = upload_response.headers["location"]

    response = client.get(detail_url)
    assert response.status_code == 200
    assert "Detail Page Subject" in response.text
    assert "Please see the attached notice." in response.text
    assert "No attachments on this message." in response.text


def test_communication_detail_unknown_id_returns_404(client: TestClient):
    response = client.get("/communications/email/999999")
    assert response.status_code == 404


def test_communications_home_lists_uploaded_email(client: TestClient):
    case_id = _create_case(client)
    client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_bytes(subject="Shows In List"), "message/rfc822")},
        follow_redirects=False,
    )

    response = client.get("/communications")
    assert "Shows In List" in response.text


def test_upload_email_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.post(
        "/communications/upload",
        data={"case_id": "1"},
        files={"file": ("notice.eml", _eml_bytes(), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_communication_detail_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications/email/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- email threads (Communications Phase Step 4) -----------------------


def _upload_eml(client: TestClient, case_id: int, **kwargs) -> str:
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": (kwargs.pop("filename", "notice.eml"), _eml_bytes(**kwargs), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_threads_page_shows_empty_state_with_no_threads(client: TestClient):
    response = client.get("/communications/threads")
    assert response.status_code == 200
    assert "No email threads yet" in response.text


def test_lone_message_has_no_thread_link_on_detail_page(client: TestClient):
    case_id = _create_case(client)
    detail_url = _upload_eml(client, case_id, subject="Solo Message", message_id="<solo@example.org>")

    response = client.get(detail_url)
    assert response.status_code == 200
    assert "/communications/threads/" not in response.text


def test_reply_chain_appears_in_thread_list_and_detail(client: TestClient, app: FastAPI):
    from app.db.models import Communication

    case_id = _create_case(client)
    parent_url = _upload_eml(
        client, case_id, subject="IEP Meeting", message_id="<parent@example.org>", filename="parent.eml"
    )
    parent_id = int(parent_url.rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        parent = db.get(Communication, parent_id)
        assert parent.thread_id is None  # not yet a thread -- only one message so far

    reply_response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={
            "file": (
                "reply.eml",
                (
                    b"From: parent@yahoo.com\n"
                    b"To: teacher@district.example.org\n"
                    b"Subject: Re: IEP Meeting\n"
                    b"Date: Tue, 8 Mar 2022 09:00:00 -0500\n"
                    b"Message-ID: <reply@example.org>\n"
                    b"In-Reply-To: <parent@example.org>\n"
                    b"\n"
                    b"Sounds good.\n"
                ),
                "message/rfc822",
            )
        },
        follow_redirects=False,
    )
    assert reply_response.status_code == 303

    # The thread now shows on the list page.
    threads_page = client.get("/communications/threads")
    assert "IEP Meeting" in threads_page.text
    assert "2" in threads_page.text  # message_count column

    # The parent's detail page now has a working View Thread link.
    parent_detail = client.get(parent_url)
    assert "View Thread" in parent_detail.text

    with app.state.session_factory() as db:
        parent = db.get(Communication, parent_id)
        thread_id = parent.thread_id
    assert thread_id is not None

    thread_detail = client.get(f"/communications/threads/{thread_id}")
    assert thread_detail.status_code == 200
    assert "IEP Meeting" in thread_detail.text
    # Both original messages remain independently viewable.
    assert f'href="/communications/email/{parent_id}"' in thread_detail.text


def test_thread_detail_unknown_id_returns_404(client: TestClient):
    response = client.get("/communications/threads/999999")
    assert response.status_code == 404


def test_threads_page_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications/threads", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_thread_detail_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications/threads/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- attachment review / promotion to Documents (Communications Phase Step 5) --


def _eml_with_attachment_bytes(
    *, message_id: str = "<att-1@example.org>", subject: str = "2024 IEP", filename: str = "2024 IEP.pdf"
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


def _upload_email_with_attachment(client: TestClient, case_id: int, **kwargs) -> int:
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", _eml_with_attachment_bytes(**kwargs), "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _get_attachment_id(app: FastAPI, communication_id: int) -> int:
    from app.db.models import CommunicationAttachment

    with app.state.session_factory() as db:
        return db.scalars(
            select(CommunicationAttachment).where(CommunicationAttachment.communication_id == communication_id)
        ).one().attachment_id


def test_attachment_review_page_shows_suggestions(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id, filename="2024 IEP.pdf")
    attachment_id = _get_attachment_id(app, communication_id)

    response = client.get(f"/communications/attachments/{attachment_id}/review")
    assert response.status_code == 200
    assert "IEP" in response.text
    assert "Amanda Wagner" in response.text  # suggested Source
    assert "2022-03-07" in response.text  # suggested Date received
    assert "Received as an attachment to email from Amanda Wagner" in response.text  # suggested Notes


def test_attachment_review_view_attachment_link(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)

    review = client.get(f"/communications/attachments/{attachment_id}/review")
    assert f"/communications/attachments/{attachment_id}/file" in review.text

    file_response = client.get(f"/communications/attachments/{attachment_id}/file")
    assert file_response.status_code == 200


def test_add_attachment_to_documents_creates_document(client: TestClient, app: FastAPI):
    from app.db.models import Document

    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)

    response = client.post(
        f"/communications/attachments/{attachment_id}/add-to-documents",
        data={"source": "Amanda Wagner", "date_received": "2022-03-07", "notes": "Received via email."},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Added to Documents" in response.text

    with app.state.session_factory() as db:
        document = db.scalars(select(Document)).one()
        assert document.source == "Amanda Wagner"
        assert document.notes == "Received via email."


def test_add_duplicate_attachment_surfaces_already_in_ferchronos(client: TestClient, app: FastAPI):
    case_id = _create_case(client)

    # First email/attachment -- promotes to a brand-new Document.
    communication_id_a = _upload_email_with_attachment(client, case_id, message_id="<dup-a@x>")
    attachment_id_a = _get_attachment_id(app, communication_id_a)
    client.post(f"/communications/attachments/{attachment_id_a}/add-to-documents", data={}, follow_redirects=False)

    # Second email, identical attachment bytes -- must link, not duplicate.
    communication_id_b = _upload_email_with_attachment(client, case_id, message_id="<dup-b@x>")
    attachment_id_b = _get_attachment_id(app, communication_id_b)
    response = client.post(
        f"/communications/attachments/{attachment_id_b}/add-to-documents", data={}, follow_redirects=False
    )

    assert response.status_code == 200
    assert "already in FERChronos" in response.text

    from app.db.models import Document

    with app.state.session_factory() as db:
        assert db.query(Document).count() == 1


def test_exclude_attachment_route(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)

    response = client.post(f"/communications/attachments/{attachment_id}/exclude", follow_redirects=False)
    assert response.status_code == 303

    review = client.get(f"/communications/attachments/{attachment_id}/review")
    assert "excluded" in review.text.lower()

    from app.db.models import Document

    with app.state.session_factory() as db:
        assert db.query(Document).count() == 0


def test_leave_with_email_route(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)

    response = client.post(
        f"/communications/attachments/{attachment_id}/leave-with-email", follow_redirects=False
    )
    assert response.status_code == 303

    review = client.get(f"/communications/attachments/{attachment_id}/review")
    assert "left with the email" in review.text.lower()


def test_communication_detail_shows_linked_document(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)
    client.post(f"/communications/attachments/{attachment_id}/add-to-documents", data={}, follow_redirects=False)

    response = client.get(f"/communications/email/{communication_id}")
    assert "2024 IEP.pdf" in response.text


def test_document_detail_shows_multiple_originating_emails(client: TestClient, app: FastAPI):
    case_id = _create_case(client)

    communication_id_a = _upload_email_with_attachment(client, case_id, message_id="<multi-a@x>")
    attachment_id_a = _get_attachment_id(app, communication_id_a)
    client.post(f"/communications/attachments/{attachment_id_a}/add-to-documents", data={}, follow_redirects=False)

    communication_id_b = _upload_email_with_attachment(client, case_id, message_id="<multi-b@x>")
    attachment_id_b = _get_attachment_id(app, communication_id_b)
    client.post(f"/communications/attachments/{attachment_id_b}/add-to-documents", data={}, follow_redirects=False)

    from app.db.models import CommunicationAttachment

    with app.state.session_factory() as db:
        document_id = db.get(CommunicationAttachment, attachment_id_a).resulting_document_id

    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert "Originating Emails (2)" in response.text


def test_user_edited_source_is_not_overwritten_by_suggestion(client: TestClient, app: FastAPI):
    from app.db.models import Document

    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id)
    attachment_id = _get_attachment_id(app, communication_id)

    response = client.post(
        f"/communications/attachments/{attachment_id}/add-to-documents",
        data={"source": "Manually Typed Source", "source_source": "edited"},
        follow_redirects=False,
    )
    assert response.status_code == 200

    with app.state.session_factory() as db:
        document = db.scalars(select(Document)).one()
        assert document.source == "Manually Typed Source"


def test_attachment_review_unknown_id_returns_404(client: TestClient):
    response = client.get("/communications/attachments/999999/review")
    assert response.status_code == 404


def test_attachment_review_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications/attachments/1/review", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_add_to_documents_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.post(
        "/communications/attachments/1/add-to-documents", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- Communications search (Communications Phase Step 6) -------------------


def test_search_page_shows_empty_form_with_no_query(client: TestClient):
    response = client.get("/communications/search")
    assert response.status_code == 200
    assert "Search Communications" in response.text
    assert "Results" not in response.text  # no results section until something is searched


def test_search_by_free_text_finds_uploaded_email(client: TestClient):
    case_id = _create_case(client)
    _upload_eml(client, case_id, subject="Annual IEP Review Meeting", message_id="<search-1@example.org>")

    response = client.get("/communications/search", params={"q": "Annual IEP Review"})
    assert response.status_code == 200
    assert "Annual IEP Review Meeting" in response.text
    assert "Results (1)" in response.text


def test_search_no_match_shows_empty_state(client: TestClient):
    case_id = _create_case(client)
    _upload_eml(client, case_id, subject="Unrelated Subject", message_id="<search-2@example.org>")

    response = client.get("/communications/search", params={"q": "nonexistent aardvark topic"})
    assert response.status_code == 200
    assert "No matches found" in response.text


def test_search_by_sender_filter(client: TestClient):
    case_id = _create_case(client)
    _upload_eml(client, case_id, subject="From Teacher", message_id="<search-3@example.org>")

    response = client.get("/communications/search", params={"sender": "amanda.wagner"})
    assert response.status_code == 200
    assert "From Teacher" in response.text


def test_search_result_links_to_communication_detail(client: TestClient):
    case_id = _create_case(client)
    communication_id = _upload_email_with_attachment(client, case_id, message_id="<search-4@x>")

    response = client.get("/communications/search", params={"q": "2024 IEP"})
    assert f"/communications/email/{communication_id}" in response.text


def test_search_result_shows_thread_link_when_threaded(client: TestClient):
    case_id = _create_case(client)
    client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={
            "file": (
                "parent.eml",
                (
                    b"From: teacher@district.example.org\n"
                    b"To: parent@yahoo.com\n"
                    b"Subject: Threaded Search Topic\n"
                    b"Date: Mon, 7 Mar 2022 09:00:00 -0500\n"
                    b"Message-ID: <search-thr-p@x>\n\n"
                    b"Starting.\n"
                ),
                "message/rfc822",
            )
        },
        follow_redirects=False,
    )
    client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={
            "file": (
                "reply.eml",
                (
                    b"From: parent@yahoo.com\n"
                    b"To: teacher@district.example.org\n"
                    b"Subject: Re: Threaded Search Topic\n"
                    b"Date: Tue, 8 Mar 2022 09:00:00 -0500\n"
                    b"Message-ID: <search-thr-r@x>\n"
                    b"In-Reply-To: <search-thr-p@x>\n\n"
                    b"Replying.\n"
                ),
                "message/rfc822",
            )
        },
        follow_redirects=False,
    )

    response = client.get("/communications/search", params={"q": "Threaded Search Topic"})
    assert "/communications/threads/" in response.text


def test_search_malformed_query_does_not_500(client: TestClient):
    case_id = _create_case(client)
    _upload_eml(client, case_id, subject="Safe Content", message_id="<search-5@example.org>")

    for dangerous in ['"unterminated', "NEAR(a b)", "'; DROP TABLE communications; --", "***"]:
        response = client.get("/communications/search", params={"q": dangerous})
        assert response.status_code == 200, (dangerous, response.text[:300])


def test_search_page_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/communications/search", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_communications_search_does_not_change_document_search_route(client: TestClient):
    """Communications Phase Step 6: a brand-new, independent route --
    /cases/{id}/search (Document search) must be completely unaffected.
    """
    case_id = _create_case(client)
    upload_response = client.post(
        f"/cases/{case_id}/documents",
        data={},
        files={"file": ("doc.txt", b"A Prior Written Notice about placement.", "text/plain")},
        follow_redirects=False,
    )
    assert upload_response.status_code == 303

    response = client.get(f"/cases/{case_id}/search", params={"q": "Prior Written Notice"})
    assert response.status_code == 200
    assert "Results (1)" in response.text


# --- timeline suggestions (Communications Phase Step 7) ---------------------


def _upload_dated_eml(client: TestClient, case_id: int, *, message_id: str, subject: str = "IEP Meeting Notice") -> int:
    content = (
        f"From: Amanda Wagner <amanda.wagner@district.example.org>\n"
        f"To: parent@yahoo.com\n"
        f"Subject: {subject}\n"
        f"Date: Mon, 7 Mar 2022 14:30:00 -0500\n"
        f"Message-ID: {message_id}\n\n"
        f"Body text.\n"
    ).encode("utf-8")
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", content, "message/rfc822")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_communication_detail_shows_pending_timeline_suggestion(client: TestClient):
    case_id = _create_case(client)
    communication_id = _upload_dated_eml(client, case_id, message_id="<ts-1@x>")

    response = client.get(f"/communications/email/{communication_id}")
    assert response.status_code == 200
    assert "Timeline Suggestion" in response.text
    assert "Pending" in response.text
    assert "Email received from Amanda Wagner" in response.text


def test_communication_detail_shows_no_suggestion_when_no_date(client: TestClient):
    case_id = _create_case(client)
    content = (
        b"From: sender@example.org\n"
        b"To: parent@yahoo.com\n"
        b"Subject: No Date Here\n"
        b"Message-ID: <ts-nodate@x>\n\n"
        b"Body.\n"
    )
    response = client.post(
        "/communications/upload",
        data={"case_id": str(case_id)},
        files={"file": ("notice.eml", content, "message/rfc822")},
        follow_redirects=False,
    )
    communication_id = int(response.headers["location"].rsplit("/", 1)[-1])

    detail = client.get(f"/communications/email/{communication_id}")
    assert "No timeline suggestion" in detail.text


def test_approve_timeline_suggestion_from_communication_detail_page(client: TestClient, app: FastAPI):
    from app.db.models import VerifiedFact

    case_id = _create_case(client)
    communication_id = _upload_dated_eml(client, case_id, message_id="<ts-2@x>")

    response = client.post(
        f"/cases/{case_id}/facts/observations/1/promote",
        data={"confidence_label": "certain"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/communications/email/{communication_id}")
    assert "Accepted" in detail.text

    with app.state.session_factory() as db:
        fact = db.query(VerifiedFact).filter_by(communication_id=communication_id).one()
        assert fact.confidence_label == "certain"


def test_reject_timeline_suggestion_from_communication_detail_page(client: TestClient):
    case_id = _create_case(client)
    communication_id = _upload_dated_eml(client, case_id, message_id="<ts-3@x>")

    response = client.post(
        f"/cases/{case_id}/facts/observations/1/reject",
        data={"reason": "not needed"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/communications/email/{communication_id}")
    assert "Rejected" in detail.text


def test_facts_review_page_shows_view_source_email_link(client: TestClient):
    case_id = _create_case(client)
    communication_id = _upload_dated_eml(client, case_id, message_id="<ts-4@x>")

    response = client.get(f"/cases/{case_id}/facts")
    assert response.status_code == 200
    assert f"/communications/email/{communication_id}" in response.text
    assert "View Source Email" in response.text


def test_edit_and_approve_via_facts_route_preserves_edited_statement(client: TestClient, app: FastAPI):
    from app.db.models import VerifiedFact

    case_id = _create_case(client)
    _upload_dated_eml(client, case_id, message_id="<ts-5@x>")

    client.post(
        f"/cases/{case_id}/facts/observations/1/promote",
        data={"confidence_label": "probable", "statement": "Edited statement text."},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        fact = db.query(VerifiedFact).one()
        assert fact.statement == "Edited statement text."
        assert fact.confidence_label == "probable"


def test_existing_document_facts_workflow_still_works_unchanged(client: TestClient, app: FastAPI):
    """Communications Phase Step 7 must not weaken the Document-sourced
    facts pipeline in any way.
    """
    from app.db.models import Document

    case_id = _create_case(client)
    upload_response = client.post(
        f"/cases/{case_id}/documents",
        data={},
        files={"file": ("doc.txt", b"The meeting is scheduled for March 7, 2022.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    scan_response = client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)
    assert scan_response.status_code == 303

    facts_page = client.get(f"/cases/{case_id}/facts")
    assert facts_page.status_code == 200
    assert "March" in facts_page.text or "2022" in facts_page.text
