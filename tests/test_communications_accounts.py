"""Tests for app/core/communications/accounts.py -- connect/disconnect
lifecycle (Communications Phase Step 2).
"""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.errors
import pytest
from sqlalchemy.orm import Session

from app.core.communications import credentials
from app.core.communications.accounts import connect_yahoo_account, disconnect_account
from app.db.models import CommunicationAccount


class _InMemoryKeyring(keyring.backend.KeyringBackend):
    """Same minimal fake used in test_communications_credentials.py --
    duplicated locally rather than imported cross-file, since each test
    module owns its own fixtures.
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
    test_communications_credentials.py's module docstring for why this
    is set deliberately per-test rather than relied upon as an ambient
    environment default.
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


def test_connect_yahoo_account_creates_row_and_stores_credential(
    db_session: Session, working_keyring
):
    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="app-pw-123", actor="test-user"
    )

    stored = db_session.get(CommunicationAccount, account.account_id)
    assert stored.provider == "yahoo"
    assert stored.email_address == "parent@yahoo.com"
    assert stored.auth_method == "app_password"
    assert stored.status == "connected"
    assert stored.created_by == "test-user"
    assert credentials.get_credential(stored.credential_ref) == "app-pw-123"


def test_connect_yahoo_account_raises_when_no_keyring_available(
    db_session: Session, no_keyring_available
):
    with pytest.raises(credentials.KeyringUnavailableError):
        connect_yahoo_account(
            db_session, email_address="parent@yahoo.com", app_password="app-pw-123", actor="test-user"
        )

    assert db_session.query(CommunicationAccount).count() == 0


def test_connect_yahoo_account_never_writes_the_secret_to_the_database(
    db_session: Session, working_keyring
):
    connect_yahoo_account(
        db_session,
        email_address="parent@yahoo.com",
        app_password="super-secret-app-password",
        actor="test-user",
    )

    # Inspect every column value on the row, not just the ones this test
    # happens to know about -- the secret must not appear anywhere on it.
    stored = db_session.query(CommunicationAccount).one()
    for column in CommunicationAccount.__table__.columns:
        value = getattr(stored, column.name)
        assert value != "super-secret-app-password"
        if isinstance(value, str):
            assert "super-secret-app-password" not in value
    # credential_ref is the one column that legitimately references the
    # credential -- but only by opaque key, never by value.
    assert stored.credential_ref != "super-secret-app-password"


def test_disconnect_account_deletes_credential_and_marks_disconnected(
    db_session: Session, working_keyring
):
    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="app-pw-123", actor="test-user"
    )
    credential_ref = account.credential_ref

    disconnect_account(db_session, account)

    stored = db_session.get(CommunicationAccount, account.account_id)
    assert stored.status == "disconnected"
    assert stored.disconnected_at is not None
    assert credentials.get_credential(credential_ref) is None


def test_disconnect_account_preserves_communications_and_documents(
    db_session: Session, working_keyring, sample_case
):
    """docs/COMMUNICATIONS_PLAN.md §16: disconnecting must never touch
    already-imported data.
    """
    from app.db.models import Communication

    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="app-pw-123", actor="test-user"
    )
    communication = Communication(
        account_id=account.account_id,
        case_id=sample_case.case_id,
        subject="IEP meeting",
        sha256_hash="a" * 64,
        stored_path="cases/1/communications/aa/message.eml",
        file_size_bytes=100,
        import_method="imap_sync",
        imported_by="test-user",
    )
    db_session.add(communication)
    db_session.commit()

    disconnect_account(db_session, account)

    still_there = db_session.get(Communication, communication.communication_id)
    assert still_there is not None
    assert still_there.subject == "IEP meeting"
    assert still_there.deleted_at is None


def test_reconnecting_same_address_reactivates_existing_account(
    db_session: Session, working_keyring
):
    """Step 12: reconnecting a previously-disconnected Yahoo address must
    reuse the same `account_id`, not fork a second logical account --
    this is what keeps Step 10's account-scoped duplicate-import
    detection, and every existing batch/attachment/provenance row's
    `account_id` foreign key, pointed at the account's real history.
    """
    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="first-app-pw", actor="test-user"
    )
    original_account_id = account.account_id
    disconnect_account(db_session, account)

    reconnected = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="second-app-pw", actor="test-user"
    )

    assert reconnected.account_id == original_account_id
    assert db_session.query(CommunicationAccount).count() == 1
    stored = db_session.get(CommunicationAccount, original_account_id)
    assert stored.status == "connected"
    assert stored.disconnected_at is None
    assert credentials.get_credential(stored.credential_ref) == "second-app-pw"


def test_reconnecting_same_address_case_insensitive(db_session: Session, working_keyring):
    account = connect_yahoo_account(
        db_session, email_address="Parent@Yahoo.com", app_password="first-app-pw", actor="test-user"
    )
    disconnect_account(db_session, account)

    reconnected = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="second-app-pw", actor="test-user"
    )

    assert reconnected.account_id == account.account_id
    assert db_session.query(CommunicationAccount).count() == 1


def test_reconnecting_preserves_prior_imports_account_scope(db_session: Session, working_keyring, sample_case):
    """The account-scoped duplicate-detection guarantee Step 10 depends
    on: a Communication imported before a disconnect/reconnect cycle
    keeps the exact same `account_id` the reconnected account now has,
    so a later re-import of the same message is still detected as a
    duplicate against it.
    """
    from app.db.models import Communication

    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="first-app-pw", actor="test-user"
    )
    communication = Communication(
        account_id=account.account_id,
        case_id=sample_case.case_id,
        subject="IEP meeting",
        sha256_hash="a" * 64,
        stored_path="cases/1/communications/aa/message.eml",
        file_size_bytes=100,
        import_method="imap_sync",
        imported_by="test-user",
    )
    db_session.add(communication)
    db_session.commit()

    disconnect_account(db_session, account)
    reconnected = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="second-app-pw", actor="test-user"
    )

    db_session.refresh(communication)
    assert communication.account_id == reconnected.account_id


def test_connecting_a_different_address_creates_a_separate_account(db_session: Session, working_keyring):
    first = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="pw-1", actor="test-user"
    )
    second = connect_yahoo_account(
        db_session, email_address="other-parent@yahoo.com", app_password="pw-2", actor="test-user"
    )

    assert first.account_id != second.account_id
    assert db_session.query(CommunicationAccount).count() == 2


def test_reconnecting_an_already_connected_account_is_safe(db_session: Session, working_keyring):
    """Submitting Connect Yahoo again for an already-connected account
    (e.g. to rotate the app password) reactivates in place rather than
    erroring or forking a duplicate row.
    """
    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="first-app-pw", actor="test-user"
    )

    reconnected = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="rotated-app-pw", actor="test-user"
    )

    assert reconnected.account_id == account.account_id
    assert db_session.query(CommunicationAccount).count() == 1
    assert credentials.get_credential(reconnected.credential_ref) == "rotated-app-pw"


def test_disconnect_is_safe_to_call_twice(db_session: Session, working_keyring):
    account = connect_yahoo_account(
        db_session, email_address="parent@yahoo.com", app_password="app-pw-123", actor="test-user"
    )

    disconnect_account(db_session, account)
    disconnect_account(db_session, account)  # must not raise

    stored = db_session.get(CommunicationAccount, account.account_id)
    assert stored.status == "disconnected"
