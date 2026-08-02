"""Tests for app/core/auth/passwords.py -- Argon2id password/recovery-key
hashing (Security Phase Step 1).
"""

from __future__ import annotations

from app.core.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
    verify_password_constant_time,
)


def test_hash_password_returns_argon2id_encoded_string():
    result = hash_password("correct horse battery staple")
    assert result.startswith("$argon2id$")
    assert "correct horse battery staple" not in result


def test_hash_password_is_salted_and_non_deterministic():
    first = hash_password("same password")
    second = hash_password("same password")
    assert first != second
    # Both remain independently valid despite differing.
    assert verify_password("same password", first)
    assert verify_password("same password", second)


def test_verify_password_accepts_correct_password():
    hashed = hash_password("owner-password-123")
    assert verify_password("owner-password-123", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("owner-password-123")
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_rejects_malformed_hash_without_raising():
    assert verify_password("anything", "not-a-real-argon2-hash") is False


def test_verify_password_constant_time_returns_false_when_no_account_yet():
    """Before first-run setup there is no AppAuth row, so
    password_hash is None -- this must never raise and must never
    report success.
    """
    assert verify_password_constant_time("any-password", None) is False


def test_verify_password_constant_time_matches_verify_password_when_hash_given():
    hashed = hash_password("owner-password-123")
    assert verify_password_constant_time("owner-password-123", hashed) is True
    assert verify_password_constant_time("wrong-password", hashed) is False


def test_needs_rehash_is_false_for_a_freshly_hashed_password():
    """A hash produced by the current hasher's own parameters should
    never claim to need rehashing against itself.
    """
    hashed = hash_password("owner-password-123")
    assert needs_rehash(hashed) is False
