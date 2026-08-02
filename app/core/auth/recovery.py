"""Recovery-key generation and normalization (Security Phase Step 5).

A recovery key is this application's only way to reset a forgotten
password -- there are deliberately no security questions, password
hints, hidden master password, or cloud-backed recovery of any kind (see
docs/PRIVACY_SECURITY.md and the Security Phase plan): those are all
either guessable/weak or would mean this local-only, no-network
application phoning home. Losing the password *and* the recovery key
means there is no way back in -- once whole-database encryption ships,
that will mean the vault itself is permanently unreadable, not just the
web UI locked; see app/web/templates/auth_recovery_key.html for the
warning shown to the owner every time a key is displayed.

Hashing/verifying the key itself reuses
app.core.auth.passwords.hash_password() /
verify_password_constant_time() unchanged -- a recovery key is just
another high-entropy secret string as far as that module is concerned
(see AppAuth.recovery_key_hash's docstring, which already anticipated
this). This module only handles generating a key formatted for a human
to transcribe, and normalizing what they type back in.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.core.auth.passwords import hash_password

if TYPE_CHECKING:
    from app.db.models import AppAuth

# Crockford-base32-style alphabet, deliberately missing the characters
# people most often confuse when copying a code by hand: 0/O, 1/I/L, and
# U (commonly misread as V). 30 symbols -> ~4.9 bits/char.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUP_COUNT = 6
_GROUP_LENGTH = 4
_KEY_LENGTH = _GROUP_COUNT * _GROUP_LENGTH  # 24 chars -> ~118 bits of entropy


def generate_recovery_key() -> str:
    """A fresh, cryptographically random recovery key, formatted as
    6 groups of 4 characters separated by dashes (e.g.
    "K7M4-9XPQ-..."). Never derived from anything guessable (not the
    password, not a timestamp) -- `secrets.choice` per character.
    """
    chars = [secrets.choice(_ALPHABET) for _ in range(_KEY_LENGTH)]
    groups = ["".join(chars[i : i + _GROUP_LENGTH]) for i in range(0, _KEY_LENGTH, _GROUP_LENGTH)]
    return "-".join(groups)


def normalize_recovery_key(raw: str) -> str:
    """Strip whitespace/dashes and uppercase, so the owner can type the
    key back with or without the dashes, in any case, and it still
    matches what `generate_recovery_key()` produced. Hashing and
    verification both always go through this first -- the raw,
    as-displayed format is never what's hashed or compared directly.
    """
    return "".join(ch for ch in raw.strip().upper() if ch.isalnum())


def issue_recovery_key(auth: "AppAuth") -> str:
    """Generate a fresh recovery key, hash it into `auth.recovery_key_hash`
    and stamp `recovery_key_updated_at`, and return the raw key for
    exactly one display. Shared by every issuance path -- first-run setup
    (app.api.auth.post_setup), a successful recovery
    (app.api.auth.post_recover), and an authenticated owner's deliberate
    rotation (app.api.account.post_regenerate_recovery_key) -- so there
    is exactly one place that decides what "issuing a new key" means.
    Never returns the same key twice: each call mints a brand-new one and
    immediately invalidates whatever key existed before by overwriting
    its hash. Does not commit; the caller controls the transaction
    boundary.
    """
    raw_key = generate_recovery_key()
    auth.recovery_key_hash = hash_password(normalize_recovery_key(raw_key))
    auth.recovery_key_updated_at = datetime.now(timezone.utc)
    return raw_key
