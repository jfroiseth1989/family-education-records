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
