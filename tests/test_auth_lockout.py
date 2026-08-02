"""Tests for local failed-login throttling (Security Phase Step 4):
escalating lockouts after repeated failures, and the successful-login
reset. Uses `anonymous_client` throughout -- this is specifically about
the pre-authenticated login flow itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.auth import _LOCKOUT_MAX_SECONDS, _LOCKOUT_MESSAGE, _lockout_seconds_for
from app.core.auth.session import SESSION_COOKIE_NAME
from app.db.models import AppAuth

_PASSWORD = "owner-password-123"


def _csrf_token(client: TestClient) -> str:
    client.get("/auth/setup")
    return client.cookies.get("csrf_token")


def _complete_setup(client: TestClient) -> None:
    """Create the account (Security Phase Step 5's response is the
    one-time recovery-key display, not a redirect -- see
    app/api/auth.py::post_setup), then immediately log the resulting
    session back out, since every test here only cares about the account
    existing, not staying signed in.
    """
    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/setup",
        data={"password": _PASSWORD, "confirm_password": _PASSWORD, "csrf_token": csrf_token},
    )
    assert response.status_code == 200
    client.cookies.clear()  # log the setup-created session back out, as a fresh visitor


def _get_auth(app: FastAPI) -> AppAuth:
    with app.state.session_factory() as db:
        return db.scalars(select(AppAuth)).first()


def _attempt_login(client: TestClient, password: str = "wrong-password") -> object:
    csrf_token = _csrf_token(client)
    return client.post(
        "/auth/login", data={"password": password, "csrf_token": csrf_token}, follow_redirects=False
    )


# --- _lockout_seconds_for (pure escalation math) ---


def test_lockout_seconds_starts_at_sixty_for_the_fifth_failure():
    assert _lockout_seconds_for(5) == 60


def test_lockout_seconds_doubles_each_failure_after_the_fifth():
    assert _lockout_seconds_for(6) == 120
    assert _lockout_seconds_for(7) == 240
    assert _lockout_seconds_for(8) == 480


def test_lockout_seconds_caps_at_fifteen_minutes():
    assert _lockout_seconds_for(9) == _LOCKOUT_MAX_SECONDS
    assert _lockout_seconds_for(20) == _LOCKOUT_MAX_SECONDS
    assert _LOCKOUT_MAX_SECONDS == 15 * 60


# --- End-to-end: four failures, no lock yet ---


def test_first_four_failures_show_generic_wrong_password_message_and_no_lock(
    anonymous_client: TestClient, app: FastAPI
):
    _complete_setup(anonymous_client)

    for _ in range(4):
        response = _attempt_login(anonymous_client)
        assert response.status_code == 401
        assert "Incorrect password." in response.text

    auth = _get_auth(app)
    assert auth.failed_login_attempts == 4
    assert auth.locked_until is None


# --- The 5th failure locks the account ---


def test_fifth_failure_locks_the_account(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)

    for _ in range(4):
        _attempt_login(anonymous_client)
    response = _attempt_login(anonymous_client)

    assert response.status_code == 429
    assert _LOCKOUT_MESSAGE in response.text
    assert "Incorrect password." not in response.text

    auth = _get_auth(app)
    assert auth.failed_login_attempts == 5
    assert auth.locked_until is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    locked_until = auth.locked_until.replace(tzinfo=None)
    assert now + timedelta(seconds=55) < locked_until < now + timedelta(seconds=65)


def test_locked_out_login_rejects_even_the_correct_password(anonymous_client: TestClient, app: FastAPI):
    """Rate limiting must block every attempt during an active lockout,
    not just further wrong guesses -- otherwise an attacker who lands on
    the right password mid-lockout would bypass the throttle entirely.
    """
    _complete_setup(anonymous_client)
    for _ in range(5):
        _attempt_login(anonymous_client)

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/login", data={"password": _PASSWORD, "csrf_token": csrf_token}, follow_redirects=False
    )

    assert response.status_code == 429
    assert _LOCKOUT_MESSAGE in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_lockout_does_not_reveal_account_details(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    for _ in range(5):
        response = _attempt_login(anonymous_client)

    # The error line rendered on the page is exactly the fixed, generic
    # message -- no attempt count, no unlock timestamp, no distinguishing
    # detail about the account leaks into it.
    assert f'<p class="hint auth-error">{_LOCKOUT_MESSAGE}</p>' in response.text


# --- Escalation continues across lockout cycles ---


def test_sixth_failure_after_the_first_lock_expires_doubles_the_duration(
    anonymous_client: TestClient, app: FastAPI
):
    _complete_setup(anonymous_client)
    for _ in range(5):
        _attempt_login(anonymous_client)

    # Fast-forward past the first (60s) lock without waiting in real time.
    with app.state.session_factory() as db:
        auth = db.scalars(select(AppAuth)).first()
        auth.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    response = _attempt_login(anonymous_client)
    assert response.status_code == 429  # the 6th failure locks again immediately

    auth = _get_auth(app)
    assert auth.failed_login_attempts == 6
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    locked_until = auth.locked_until.replace(tzinfo=None)
    assert now + timedelta(seconds=110) < locked_until < now + timedelta(seconds=130)


# --- Successful login resets everything ---


def test_successful_login_resets_failed_attempts_and_clears_lock(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    for _ in range(4):
        _attempt_login(anonymous_client)  # under the threshold, no lock yet

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/login", data={"password": _PASSWORD, "csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303

    auth = _get_auth(app)
    assert auth.failed_login_attempts == 0
    assert auth.locked_until is None


def test_successful_login_after_a_lock_expires_resets_the_counter(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    for _ in range(5):
        _attempt_login(anonymous_client)

    with app.state.session_factory() as db:
        auth = db.scalars(select(AppAuth)).first()
        auth.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/login", data={"password": _PASSWORD, "csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303

    auth = _get_auth(app)
    assert auth.failed_login_attempts == 0
    assert auth.locked_until is None
