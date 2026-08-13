"""Secure external-credential storage (Communications Phase Step 2).

FERChronos's SQLite database is currently unencrypted (see
docs/SECURITY_ENCRYPTION_AT_REST.md -- application-level encryption is
planning-only, nothing implemented). A Yahoo app password must therefore
never be stored there in any form, plaintext or otherwise -- doing so
would be a straightforward secret-at-rest regression on an already-
plaintext file. This module stores every such credential exclusively in
the operating system's own native secret store (Windows Credential
Manager, macOS Keychain, or a Linux Secret Service provider), via the
`keyring` package -- a thin, well-established wrapper with no network
calls of its own. `communication_accounts.credential_ref` holds only an
opaque lookup key into that store; the secret itself never touches this
application's database, logs, or any HTTP response.

There is deliberately no fallback storage of any kind. If no working OS
credential store is reachable, every function here either raises
`KeyringUnavailableError` or (for `is_keyring_available`) returns False
-- callers must treat that as "this feature is unavailable right now",
never as a cue to store the secret somewhere else. See
docs/COMMUNICATIONS_PLAN.md §3/§16.
"""

from __future__ import annotations

import secrets

import keyring

_SERVICE_NAME = "FERChronos Communications"
_AVAILABILITY_PROBE_KEY = "__ferchronos_keyring_availability_probe__"


class KeyringUnavailableError(RuntimeError):
    """Raised when no working OS-native secure credential store is
    reachable. Never caught anywhere in this application as a cue to
    fall back to another storage mechanism -- only to surface a clear,
    actionable message and refuse to proceed.
    """


def is_keyring_available() -> bool:
    """True iff a real, working OS-native secret store is reachable right now.

    Does a genuine round-trip -- write a throwaway probe value, read it
    back, delete it -- rather than merely checking which backend class
    `keyring` happened to select. Checking the backend *object* isn't
    enough: `keyring` falls back to a "null" backend on a system with no
    real secret-storage service (e.g. a Linux system with no Secret
    Service daemon running) that exists as a valid object but silently
    discards everything, so only an actual round-trip proves secrets are
    persisted and retrievable at all. Any exception whatsoever is treated
    as "not available" -- fail closed, never assumed working.
    """
    probe_value = secrets.token_urlsafe(16)
    try:
        keyring.set_password(_SERVICE_NAME, _AVAILABILITY_PROBE_KEY, probe_value)
        round_tripped = keyring.get_password(_SERVICE_NAME, _AVAILABILITY_PROBE_KEY)
        keyring.delete_password(_SERVICE_NAME, _AVAILABILITY_PROBE_KEY)
    except Exception:
        return False
    return round_tripped == probe_value


def new_credential_ref() -> str:
    """A fresh, opaque, unguessable key naming a credential in the OS
    secret store. Deliberately never derived from the account's email
    address, provider, or database id -- this string alone (e.g. if it
    ever appeared in a log or an error message) reveals nothing about
    which account it belongs to.
    """
    return f"yahoo-{secrets.token_urlsafe(24)}"


def store_credential(credential_ref: str, secret: str) -> None:
    """Save `secret` under `credential_ref` in the OS-native secret store.

    Raises `KeyringUnavailableError` instead of ever falling back to any
    other storage. Callers are expected to check `is_keyring_available()`
    before creating a `communication_accounts` row -- `credential_ref` is
    meaningless without a secret actually stored behind it, so a caller
    must never write that row if this call fails.
    """
    try:
        keyring.set_password(_SERVICE_NAME, credential_ref, secret)
    except Exception as exc:
        raise KeyringUnavailableError(
            "No secure credential store is available on this system -- "
            "refusing to save this credential anywhere else."
        ) from exc


def get_credential(credential_ref: str) -> str | None:
    """Return the secret stored under `credential_ref`, or None if
    nothing is stored there (including "no keyring backend available" --
    treated the same as "not found" here, since a caller with no
    credential to use behaves identically either way). Not used by any
    Step 2 code path -- this exists so later steps (IMAP connectivity,
    Step 9) have a single, already-tested place to read a credential
    back from, without duplicating the storage convention.
    """
    try:
        return keyring.get_password(_SERVICE_NAME, credential_ref)
    except Exception:
        return None


def delete_credential(credential_ref: str) -> None:
    """Remove the secret stored under `credential_ref`, if any.

    Safe to call even if nothing is stored under this key (e.g. cleanup
    after a failed connect attempt, or a double-disconnect) -- treated as
    success, not an error, since the end state (nothing stored) is
    identical either way. Also safe to call when no keyring backend is
    available at all -- there is nothing to delete either way.
    """
    try:
        keyring.delete_password(_SERVICE_NAME, credential_ref)
    except Exception:
        pass
