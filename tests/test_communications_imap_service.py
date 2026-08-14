"""Tests for app/core/communications/imap_service.py -- wiring a stored
Yahoo credential to an authenticated ImapClient (Communications Phase
Step 9).
"""

from __future__ import annotations

import pytest

import app.core.communications.imap_client as imap_client_module
import app.core.communications.imap_service as imap_service_module
from app.core.communications.imap_client import ImapAuthenticationError
from app.core.communications.imap_service import open_connection
from app.db.models import CommunicationAccount

from tests.test_communications_imap_client import FakeImapTransport


def _account(**overrides) -> CommunicationAccount:
    defaults = dict(
        account_id=1,
        provider="yahoo",
        email_address="parent@yahoo.com",
        auth_method="app_password",
        credential_ref="yahoo-abc123",
        status="connected",
        created_by="test-user",
    )
    defaults.update(overrides)
    return CommunicationAccount(**defaults)


def test_open_connection_retrieves_credential_and_authenticates(monkeypatch):
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: "app-pw")
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "app-pw"))
    monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)

    client = open_connection(_account())
    assert transport.logged_in is True
    client.logout()


def test_open_connection_uses_the_account_credential_ref(monkeypatch):
    seen_refs = []

    def fake_get_credential(ref):
        seen_refs.append(ref)
        return "app-pw"

    monkeypatch.setattr(imap_service_module, "get_credential", fake_get_credential)
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "app-pw"))
    monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)

    client = open_connection(_account(credential_ref="yahoo-specific-ref"))
    assert seen_refs == ["yahoo-specific-ref"]
    client.logout()


def test_open_connection_missing_credential_raises_without_touching_network(monkeypatch):
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: None)

    def factory_that_must_not_be_called(host, port, timeout):
        raise AssertionError("must not attempt a network connection with no credential")

    monkeypatch.setattr(imap_client_module, "_default_transport_factory", factory_that_must_not_be_called)

    from app.core.communications.imap_client import ImapCredentialUnavailableError

    with pytest.raises(ImapCredentialUnavailableError):
        open_connection(_account())


def test_open_connection_never_includes_credential_in_error_message(monkeypatch):
    monkeypatch.setattr(imap_service_module, "get_credential", lambda ref: "unmistakable-secret-value")
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "some-other-password"))
    monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)

    with pytest.raises(ImapAuthenticationError) as excinfo:
        open_connection(_account())
    assert "unmistakable-secret-value" not in str(excinfo.value)
