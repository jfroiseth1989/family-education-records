"""Argon2id password/recovery-key hashing (Security Phase Step 1).

The one place in the application that touches a plaintext password or
recovery key -- every other module only ever sees the Argon2id-encoded
hash string this module produces (AppAuth.password_hash /
AppAuth.recovery_key_hash). Never logs, stores, or returns a plaintext
value beyond hashing/verifying it in memory.

Uses argon2-cffi's default `PasswordHasher()` parameters (time_cost=3,
memory_cost=64 MiB, parallelism=4 as of this writing) -- Argon2id is the
OWASP-recommended choice and the one the Security Phase plan calls for.
Tunable later via `needs_rehash()` without a schema change, since the
encoded hash string embeds its own parameters.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

# A precomputed hash of a value nobody will ever legitimately type, used
# only so verify_password_constant_time() takes the same Argon2
# verification path (and roughly the same wall-clock time) whether or
# not a real password_hash exists yet -- so a login attempt's response
# time can't be used to infer whether first-run setup has happened.
_DUMMY_HASH = _hasher.hash("no-account-configured-yet")


def hash_password(password: str) -> str:
    """Hash `password` with Argon2id, returning a self-describing encoded
    hash string (algorithm, parameters, salt, and hash all embedded --
    see argon2-cffi's format) suitable for storing in
    AppAuth.password_hash or AppAuth.recovery_key_hash.
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True iff `password` matches `password_hash`.

    Delegates to argon2-cffi's own verify(), which performs a
    constant-time comparison of the computed digest against the stored
    one -- this function never does its own (potentially timing-leaky)
    string comparison. A malformed/foreign hash string is treated the
    same as "wrong password" (returns False) rather than raising, so
    every caller has exactly one failure path to handle.
    """
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHash):
        return False
    return True


def verify_password_constant_time(password: str, password_hash: str | None) -> bool:
    """Like verify_password(), but safe to call even before any account
    exists yet (`password_hash` is None -- no AppAuth row created by
    first-run setup).

    Always performs exactly one Argon2 verification either way --
    against `password_hash` if given, or against a fixed dummy hash if
    not -- so a caller (the login route, in a later step) never reveals
    through response timing whether the app has been set up yet. Always
    returns False when `password_hash` is None: there is nothing to
    match against.
    """
    if password_hash is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, password_hash)


def needs_rehash(password_hash: str) -> bool:
    """True if `password_hash` was hashed with weaker-than-current
    Argon2id parameters and should be re-hashed the next time its
    plaintext is available (i.e. immediately after a successful
    verify_password() call) -- argon2-cffi's own recommended pattern for
    migrating stored hashes forward when parameters change.
    """
    return _hasher.check_needs_rehash(password_hash)
