"""Tests for app/core/auth/recovery.py -- recovery-key generation,
normalization, and issuance (Security Phase Step 5).
"""

from __future__ import annotations

from app.core.auth.passwords import verify_password
from app.core.auth.recovery import generate_recovery_key, issue_recovery_key, normalize_recovery_key
from app.db.models import AppAuth


def test_generate_recovery_key_has_the_expected_shape():
    key = generate_recovery_key()
    groups = key.split("-")
    assert len(groups) == 6
    assert all(len(group) == 4 for group in groups)


def test_generate_recovery_key_uses_only_the_unambiguous_alphabet():
    key = generate_recovery_key()
    normalized = key.replace("-", "")
    for forbidden_char in ("0", "O", "1", "I", "L", "U"):
        assert forbidden_char not in normalized


def test_generate_recovery_key_is_random_across_calls():
    first = generate_recovery_key()
    second = generate_recovery_key()
    assert first != second


def test_normalize_recovery_key_strips_dashes_and_uppercases():
    assert normalize_recovery_key("abcd-1234-efgh") == "ABCD1234EFGH"


def test_normalize_recovery_key_strips_surrounding_whitespace():
    assert normalize_recovery_key("  ABCD-1234  ") == "ABCD1234"


def test_normalize_recovery_key_is_idempotent_regardless_of_input_formatting():
    key = generate_recovery_key()
    assert normalize_recovery_key(key) == normalize_recovery_key(key.lower())
    assert normalize_recovery_key(key) == normalize_recovery_key(key.replace("-", " "))


def test_issue_recovery_key_stores_a_hash_not_the_raw_key():
    auth = AppAuth(password_hash="unused")
    raw_key = issue_recovery_key(auth)

    assert auth.recovery_key_hash is not None
    assert raw_key not in auth.recovery_key_hash
    assert verify_password(normalize_recovery_key(raw_key), auth.recovery_key_hash)


def test_issue_recovery_key_stamps_recovery_key_updated_at():
    auth = AppAuth(password_hash="unused")
    assert auth.recovery_key_updated_at is None
    issue_recovery_key(auth)
    assert auth.recovery_key_updated_at is not None


def test_issue_recovery_key_invalidates_the_previous_key():
    auth = AppAuth(password_hash="unused")
    first_key = issue_recovery_key(auth)
    second_key = issue_recovery_key(auth)

    assert first_key != second_key
    assert not verify_password(normalize_recovery_key(first_key), auth.recovery_key_hash)
    assert verify_password(normalize_recovery_key(second_key), auth.recovery_key_hash)
