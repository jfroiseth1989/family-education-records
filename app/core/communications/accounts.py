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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.communications.credentials import (
    KeyringUnavailableError,
    delete_credential,
    is_keyring_available,
    new_credential_ref,
    store_credential,
)
from app.db.models import CommunicationAccount


def _find_existing_account(db: Session, *, provider: str, email_address: str) -> CommunicationAccount | None:
    """The same logical mailbox's existing row, if one was ever connected
    before -- matched case-insensitively on `provider`/`email_address`
    (Yahoo addresses aren't meaningfully case-sensitive in practice, and a
    user retyping the same address with different casing should still
    reactivate the same account, not fork a second one). If more than one
    row matches (e.g. from before this reactivation behavior existed),
    the most recently connected one is preferred -- it's the one most
    likely to hold the account's real import history.
    """
    return db.scalars(
        select(CommunicationAccount)
        .where(
            CommunicationAccount.provider == provider,
            func.lower(CommunicationAccount.email_address) == email_address.lower(),
        )
        .order_by(CommunicationAccount.connected_at.desc())
    ).first()


def connect_yahoo_account(
    db: Session, *, email_address: str, app_password: str, actor: str
) -> CommunicationAccount:
    """Save a Yahoo app-password credential and record the account.

    Raises `KeyringUnavailableError` (never falling back to any other
    storage) if no working OS-native secret store is reachable -- checked
    up front, before anything is written, so a keyring failure never
    leaves a half-created account.

    If this exact Yahoo address was already connected before (whether
    still connected or since disconnected), **reactivates that same
    account row** instead of creating a second logical account --
    updating its stored credential, `status`, and `connected_at`, and
    clearing `disconnected_at`. This matters for more than tidiness:
    `Communication.account_id` is the scope Step 10's duplicate-import
    detection keys on (see `ingestion.py::_find_duplicate_communication`)
    and every existing `CommunicationImportBatch`/attachment/provenance
    row still points at the original `account_id` -- forking a new row
    for the same mailbox would silently narrow that dedup scope to "since
    the most recent reconnect" and orphan the account's history from any
    *new* activity, without deleting or corrupting anything already
    imported. Reactivating never touches a single `Communication`,
    `CommunicationImportBatch`, or any other previously-imported row.

    The credential is written to the OS store *before* the database row
    is written; if that write then fails for any reason, a freshly
    generated credential is deleted again rather than left orphaned in
    the OS store (a reused, reactivated credential_ref is left alone on
    failure, since it may still be the one an unaffected, already-
    connected account depends on).
    """
    if not is_keyring_available():
        raise KeyringUnavailableError(
            "No secure credential store is available on this system. "
            "FERChronos never stores this credential anywhere else, so "
            "connecting a Yahoo account is unavailable until a secure "
            "credential store (Windows Credential Manager, macOS "
            "Keychain, or a Linux Secret Service provider) is reachable."
        )

    existing = _find_existing_account(db, provider="yahoo", email_address=email_address)
    credential_ref = existing.credential_ref if existing is not None else new_credential_ref()
    store_credential(credential_ref, app_password)

    try:
        if existing is not None:
            existing.status = "connected"
            existing.connected_at = datetime.now(timezone.utc)
            existing.disconnected_at = None
            db.commit()
            return existing

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
        if existing is None:
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
