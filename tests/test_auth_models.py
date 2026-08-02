"""Tests for AppAuth/AppSession (Security Phase Step 1) -- schema and
defaults only. No routes exist yet that use these tables; see
app/db/migrations/versions/f87a326f960b_add_app_auth_and_app_sessions.py
and the AppAuth/AppSession model docstrings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth.passwords import hash_password
from app.db.models import AppAuth, AppSession


def test_app_auth_table_starts_empty(db_session: Session):
    """Zero rows is exactly how the app will detect "first launch not
    completed yet" in a later step -- this locks that invariant in for
    a freshly migrated + seeded vault.
    """
    rows = db_session.scalars(select(AppAuth)).all()
    assert rows == []


def test_app_auth_row_can_be_created_with_defaults(db_session: Session):
    auth = AppAuth(
        password_hash=hash_password("owner-password-123"),
        recovery_key_hash=hash_password("some-recovery-key"),
    )
    db_session.add(auth)
    db_session.commit()

    stored = db_session.scalars(select(AppAuth)).one()
    assert stored.failed_login_attempts == 0
    assert stored.locked_until is None
    assert stored.inactivity_lock_minutes == 15
    assert stored.session_absolute_expiry_hours == 12
    assert stored.password_updated_at is None
    assert stored.recovery_key_updated_at is None
    assert stored.created_at is not None


def test_app_auth_recovery_key_hash_is_nullable(db_session: Session):
    auth = AppAuth(password_hash=hash_password("owner-password-123"))
    db_session.add(auth)
    db_session.commit()

    stored = db_session.scalars(select(AppAuth)).one()
    assert stored.recovery_key_hash is None


def test_app_auth_never_stores_plaintext(db_session: Session):
    auth = AppAuth(
        password_hash=hash_password("owner-password-123"),
        recovery_key_hash=hash_password("some-recovery-key"),
    )
    db_session.add(auth)
    db_session.commit()

    stored = db_session.scalars(select(AppAuth)).one()
    assert "owner-password-123" not in stored.password_hash
    assert "some-recovery-key" not in stored.recovery_key_hash


def test_app_sessions_table_starts_empty(db_session: Session):
    rows = db_session.scalars(select(AppSession)).all()
    assert rows == []


def test_app_session_row_can_be_created(db_session: Session):
    now = datetime.now(timezone.utc)
    session_row = AppSession(
        session_id="a" * 43,
        last_activity_at=now,
        expires_at=now + timedelta(hours=12),
    )
    db_session.add(session_row)
    db_session.commit()

    stored = db_session.get(AppSession, "a" * 43)
    assert stored is not None
    assert stored.created_at is not None


def test_app_session_rows_can_be_hard_deleted(db_session: Session):
    """Sessions are ephemeral security state, not evidence -- unlike
    every other table in this schema, hard-deleting a session row on
    logout/lock/expiry is the approved, correct behavior.
    """
    now = datetime.now(timezone.utc)
    session_row = AppSession(
        session_id="b" * 43,
        last_activity_at=now,
        expires_at=now + timedelta(hours=12),
    )
    db_session.add(session_row)
    db_session.commit()

    db_session.delete(session_row)
    db_session.commit()

    assert db_session.get(AppSession, "b" * 43) is None
