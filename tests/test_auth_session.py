"""Tests for app/core/auth/session.py -- server-side session
issuance/lookup (Security Phase Step 2). Purely informational in this
step: nothing here enforces access; see the module docstring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth.passwords import hash_password
from app.core.auth.session import (
    SESSION_COOKIE_NAME,
    create_session,
    delete_session,
    get_current_session,
    is_session_valid,
    touch_session,
)
from app.db.models import AppAuth, AppSession


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None):
        self.cookies = cookies or {}


def _make_auth(db: Session, **overrides) -> AppAuth:
    auth = AppAuth(password_hash=hash_password("owner-password-123"), **overrides)
    db.add(auth)
    db.commit()
    return auth


def test_create_session_mints_a_random_token_and_correct_expiry(db_session: Session):
    auth = _make_auth(db_session, session_absolute_expiry_hours=12)

    session = create_session(db_session, auth)
    db_session.commit()

    assert len(session.session_id) > 20
    now = datetime.now(timezone.utc)
    assert session.expires_at > now + timedelta(hours=11)
    assert session.expires_at < now + timedelta(hours=13)


def test_create_session_mints_a_different_token_each_time(db_session: Session):
    auth = _make_auth(db_session)
    first = create_session(db_session, auth)
    db_session.commit()
    second = create_session(db_session, auth)
    db_session.commit()

    assert first.session_id != second.session_id


def test_touch_session_slides_last_activity_forward():
    old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    session = AppSession(
        session_id="x" * 43,
        last_activity_at=old_time,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    touch_session(session)
    assert session.last_activity_at > old_time


def test_is_session_valid_true_for_a_fresh_session():
    auth = AppAuth(password_hash="unused", inactivity_lock_minutes=15)
    now = datetime.now(timezone.utc)
    session = AppSession(session_id="a" * 43, last_activity_at=now, expires_at=now + timedelta(hours=12))
    assert is_session_valid(session, auth) is True


def test_is_session_valid_false_past_absolute_expiry():
    auth = AppAuth(password_hash="unused", inactivity_lock_minutes=15)
    now = datetime.now(timezone.utc)
    session = AppSession(
        session_id="b" * 43,
        last_activity_at=now,
        expires_at=now - timedelta(seconds=1),
    )
    assert is_session_valid(session, auth) is False


def test_is_session_valid_false_past_inactivity_timeout():
    auth = AppAuth(password_hash="unused", inactivity_lock_minutes=15)
    now = datetime.now(timezone.utc)
    session = AppSession(
        session_id="c" * 43,
        last_activity_at=now - timedelta(minutes=16),
        expires_at=now + timedelta(hours=12),
    )
    assert is_session_valid(session, auth) is False


def test_get_current_session_returns_none_without_a_cookie(db_session: Session):
    _make_auth(db_session)
    assert get_current_session(_FakeRequest(), db_session) is None


def test_get_current_session_returns_none_for_unknown_session_id(db_session: Session):
    _make_auth(db_session)
    request = _FakeRequest({SESSION_COOKIE_NAME: "does-not-exist"})
    assert get_current_session(request, db_session) is None


def test_get_current_session_returns_none_when_no_account_exists_yet(db_session: Session):
    now = datetime.now(timezone.utc)
    session = AppSession(
        session_id="d" * 43, last_activity_at=now, expires_at=now + timedelta(hours=12)
    )
    db_session.add(session)
    db_session.commit()

    request = _FakeRequest({SESSION_COOKIE_NAME: session.session_id})
    assert get_current_session(request, db_session) is None


def test_get_current_session_returns_the_session_for_a_valid_cookie(db_session: Session):
    auth = _make_auth(db_session)
    session = create_session(db_session, auth)
    db_session.commit()

    request = _FakeRequest({SESSION_COOKIE_NAME: session.session_id})
    found = get_current_session(request, db_session)
    assert found is not None
    assert found.session_id == session.session_id


def test_get_current_session_returns_none_for_an_expired_session(db_session: Session):
    auth = _make_auth(db_session)
    now = datetime.now(timezone.utc)
    session = AppSession(
        session_id="e" * 43,
        last_activity_at=now,
        expires_at=now - timedelta(seconds=1),
    )
    db_session.add(session)
    db_session.commit()

    request = _FakeRequest({SESSION_COOKIE_NAME: session.session_id})
    assert get_current_session(request, db_session) is None


def test_delete_session_removes_the_row(db_session: Session):
    auth = _make_auth(db_session)
    session = create_session(db_session, auth)
    db_session.commit()
    session_id = session.session_id

    delete_session(db_session, session_id)
    db_session.commit()

    assert db_session.get(AppSession, session_id) is None


def test_delete_session_is_a_no_op_for_an_unknown_id(db_session: Session):
    # Must not raise.
    delete_session(db_session, "does-not-exist")
    db_session.commit()
