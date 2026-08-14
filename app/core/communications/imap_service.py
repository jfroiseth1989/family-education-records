"""Wires a `CommunicationAccount` to an authenticated `ImapClient`
(Communications Phase Step 9).

The one place this application decides *which* credential to use for a
mailbox action and turns it into a live, authenticated IMAP session.
Deliberately thin -- all protocol logic lives in `imap_client.py`, which
knows nothing about `CommunicationAccount` or the database; this module
is the only bridge between the two.
"""

from __future__ import annotations

from app.core.communications.credentials import get_credential
from app.core.communications.imap_client import (
    YAHOO_IMAP_HOST,
    YAHOO_IMAP_PORT,
    ImapClient,
    ImapCredentialUnavailableError,
)
from app.db.models import CommunicationAccount


def open_connection(account: CommunicationAccount) -> ImapClient:
    """Return a connected, authenticated `ImapClient` for `account`.

    Raises `ImapCredentialUnavailableError` if no secret is currently
    stored under this account's `credential_ref` (e.g. the OS secret
    store was cleared outside FERChronos, or the account was
    disconnected) -- never falls back to any other source. Raises
    whatever `ImapClient.connect_and_authenticate()` raises for any
    other connection failure. The caller owns the returned client's
    `logout()`.
    """
    password = get_credential(account.credential_ref)
    if password is None:
        raise ImapCredentialUnavailableError(
            "No secure credential is currently stored for this account. "
            "Reconnect this Yahoo account to continue."
        )

    client = ImapClient(YAHOO_IMAP_HOST, YAHOO_IMAP_PORT)
    client.connect_and_authenticate(account.email_address, password)
    return client
