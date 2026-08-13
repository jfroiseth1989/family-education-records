"""Tests for app/core/communications/credentials.py -- secure external-
credential storage (Communications Phase Step 2).

No secret this module handles may ever be stored anywhere but the
OS-native keyring backend -- see docs/COMMUNICATIONS_PLAN.md §3/§16.

Every test here controls the active `keyring` backend explicitly via a
fixture, rather than relying on "this sandbox happens to have no OS
secret-storage service" as an ambient default. That ambient assumption
turned out to be genuinely unsafe to lean on: `keyring.backend.
get_all_keyring()` is memoized exactly once per process and auto-selects
the highest-priority backend *class ever defined*, including a test
fake -- so a fake backend class merely existing anywhere in the same
pytest process (this file, or another test module) can silently become
the "no fixture" default depending on which test happens to trigger
keyring's lazy auto-detection first. Explicitly setting the backend in
every test (`no_keyring_available`/`working_keyring`/`broken_keyring`)
sidesteps that hazard entirely and makes every test deterministic
regardless of execution order or what other test files define.
"""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.errors
import pytest

from app.core.communications import credentials


class _InMemoryKeyring(keyring.backend.KeyringBackend):
    """A genuinely-working fake backend, only for exercising the success
    path in this test file -- never imported or shipped anywhere else in
    this application. Real deployments rely entirely on the OS's own
    secret store (Windows Credential Manager / macOS Keychain / a Linux
    Secret Service provider).
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
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


class _NoKeyringBackend(keyring.backend.KeyringBackend):
    """Explicitly simulates "no secret store available" -- the same
    behavior as `keyring`'s own built-in fail backend, but set
    deliberately rather than relied upon as an ambient default (see
    module docstring).
    """

    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend available (test double)")

    def set_password(self, service, username, password):
        raise keyring.errors.NoKeyringError("no backend available (test double)")

    def delete_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend available (test double)")


class _AlwaysFailingKeyring(keyring.backend.KeyringBackend):
    """A backend that exists (unlike `_NoKeyringBackend`, which stands in
    for "nothing configured at all") but breaks on every operation --
    simulates a misconfigured/broken real backend, distinct from "no
    backend at all".
    """

    priority = 1

    def get_password(self, service, username):
        raise RuntimeError("simulated backend failure")

    def set_password(self, service, username, password):
        raise RuntimeError("simulated backend failure")

    def delete_password(self, service, username):
        raise RuntimeError("simulated backend failure")


@pytest.fixture
def working_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(_InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(original)


@pytest.fixture
def broken_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(_AlwaysFailingKeyring())
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


def test_is_keyring_available_false_with_no_backend(no_keyring_available):
    assert credentials.is_keyring_available() is False


def test_is_keyring_available_true_with_working_backend(working_keyring):
    assert credentials.is_keyring_available() is True


def test_is_keyring_available_false_with_broken_backend(broken_keyring):
    assert credentials.is_keyring_available() is False


def test_store_and_get_credential_round_trip(working_keyring):
    ref = credentials.new_credential_ref()
    credentials.store_credential(ref, "correct-horse-battery-staple")
    assert credentials.get_credential(ref) == "correct-horse-battery-staple"


def test_get_credential_returns_none_for_unknown_ref(working_keyring):
    assert credentials.get_credential("never-stored") is None


def test_delete_credential_removes_it(working_keyring):
    ref = credentials.new_credential_ref()
    credentials.store_credential(ref, "a-secret")
    credentials.delete_credential(ref)
    assert credentials.get_credential(ref) is None


def test_delete_credential_is_safe_when_nothing_stored(working_keyring):
    credentials.delete_credential(credentials.new_credential_ref())  # must not raise


def test_delete_credential_is_safe_with_no_backend_available(no_keyring_available):
    # Deletion must not raise even though nothing could ever have been
    # stored, and no backend exists to store it in anyway.
    credentials.delete_credential("some-ref-that-was-never-stored")


def test_store_credential_raises_when_no_backend_available(no_keyring_available):
    with pytest.raises(credentials.KeyringUnavailableError):
        credentials.store_credential("some-ref", "a-secret")


def test_store_credential_raises_with_broken_backend(broken_keyring):
    with pytest.raises(credentials.KeyringUnavailableError):
        credentials.store_credential("some-ref", "a-secret")


def test_new_credential_ref_is_unique_and_opaque():
    ref_a = credentials.new_credential_ref()
    ref_b = credentials.new_credential_ref()
    assert ref_a != ref_b
    assert ref_a.startswith("yahoo-")
    # Opaque: reveals nothing recognizable about any specific account.
    assert "@" not in ref_a
