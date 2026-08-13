"""Communication account lifecycle: connect / disconnect (Communications
Phase Step 2).

"Connect Yahoo" at this stage means exactly one thing: save the app
password securely and record the account. It never tests the credential
against Yahoo, never opens an IMAP connection, never syncs or fetches
mail -- that is Step 9's job, built entirely separately, on top of this.
See docs/COMMUNICATIONS_PLAN.md Step 2/§16.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.communications.credentials import (
    KeyringUnavailableError,
    delete_credential,
    is_keyring_available,
    new_credential_ref,
    store_credential,
)
from app.db.models import CommunicationAccount


def connect_yahoo_account(
    db: Session, *, email_address: str, app_password: str, actor: str
) -> CommunicationAccount:
    """Save a Yahoo app-password credential and create its account row.

    Raises `KeyringUnavailableError` (never falling back to any other
    storage) if no working OS-native secret store is reachable -- checked
    up front, before anything is written, so a keyring failure never
    leaves a half-created account. The credential is written to the OS
    store *before* the database row is created; if creating the row then
    fails for any reason, the just-written credential is deleted again
    rather than left orphaned in the OS store under a `credential_ref`
    that names no account.
    """
    if not is_keyring_available():
        raise KeyringUnavailableError(
            "No secure credential store is available on this system. "
            "FERChronos never stores this credential anywhere else, so "
            "connecting a Yahoo account is unavailable until a secure "
            "credential store (Windows Credential Manager, macOS "
            "Keychain, or a Linux Secret Service provider) is reachable."
        )

    credential_ref = new_credential_ref()
    store_credential(credential_ref, app_password)

    try:
        account = CommunicationAccount(
            provider="yahoo",
            email_address=email_address,
            auth_method="app_password",
            credential_ref=credential_ref,
            created_by=actor,
        )
        db.add(account)
        db.commit()
    except Exception:
        db.rollback()
        delete_credential(credential_ref)
        raise

    return account


def disconnect_account(db: Session, account: CommunicationAccount) -> None:
    """Remove `account`'s stored credential and mark it disconnected.

    Never touches any `Communication`/`CommunicationAttachment`/
    `Document` row -- previously imported data survives a disconnect
    unconditionally (docs/COMMUNICATIONS_PLAN.md §16). Safe to call again
    on an already-disconnected account (the credential delete is already
    a no-op in that case; the status/timestamp are simply rewritten to
    the same disconnected state).
    """
    delete_credential(account.credential_ref)
    account.status = "disconnected"
    account.disconnected_at = datetime.now(timezone.utc)
    db.commit()
