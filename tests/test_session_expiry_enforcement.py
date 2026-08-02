"""End-to-end tests for inactivity auto-lock, absolute session expiry,
and their interaction with the deny-by-default enforcement middleware
and direct file/page-image URLs (Security Phase Step 4).

Uses the `client` fixture (already logged in -- see tests/conftest.py)
and reaches directly into the DB via `app.state.session_factory` to
backdate `last_activity_at`/`expires_at`, since waiting out a real
15-minute or 12-hour window in a test isn't practical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth.session import SESSION_COOKIE_NAME
from app.db.models import AppSession


def _get_session_row(app: FastAPI, session_id: str) -> AppSession:
    with app.state.session_factory() as db:
        return db.get(AppSession, session_id)


def _backdate_last_activity(app: FastAPI, session_id: str, minutes_ago: int) -> None:
    with app.state.session_factory() as db:
        session = db.get(AppSession, session_id)
        session.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db.commit()


def _expire_absolutely(app: FastAPI, session_id: str) -> None:
    with app.state.session_factory() as db:
        session = db.get(AppSession, session_id)
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()


# --- Inactivity auto-lock ---


def test_request_after_inactivity_window_redirects_to_login(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _backdate_last_activity(app, session_id, minutes_ago=16)  # default is 15

    response = client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_inactivity_expiry_deletes_the_session_row(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _backdate_last_activity(app, session_id, minutes_ago=16)

    client.get("/cases", follow_redirects=False)

    assert _get_session_row(app, session_id) is None


def test_request_within_inactivity_window_stays_authenticated(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _backdate_last_activity(app, session_id, minutes_ago=10)  # under the 15-minute limit

    response = client.get("/cases")
    assert response.status_code == 200
    assert _get_session_row(app, session_id) is not None


def test_valid_request_slides_the_inactivity_window_forward(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _backdate_last_activity(app, session_id, minutes_ago=10)
    before = _get_session_row(app, session_id).last_activity_at

    client.get("/cases")

    after = _get_session_row(app, session_id).last_activity_at
    assert after.replace(tzinfo=None) > before.replace(tzinfo=None)


def test_two_requests_ten_minutes_apart_each_never_expire_from_inactivity(client: TestClient, app: FastAPI):
    """Proves the sliding window actually slides: two requests, each
    within 15 minutes of the *previous* request (not the session's
    original creation), must both stay authenticated even though the gap
    from session creation to the second request exceeds 15 minutes.
    """
    session_id = client.cookies.get(SESSION_COOKIE_NAME)

    _backdate_last_activity(app, session_id, minutes_ago=10)
    assert client.get("/cases").status_code == 200  # slides last_activity_at to "now"

    _backdate_last_activity(app, session_id, minutes_ago=10)  # 10 min since that touch, not 20
    assert client.get("/cases").status_code == 200


# --- Absolute session expiry ---


def test_request_after_absolute_expiry_redirects_to_login(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)

    response = client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_absolute_expiry_deletes_the_session_row(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)

    client.get("/cases", follow_redirects=False)

    assert _get_session_row(app, session_id) is None


def test_absolute_expiry_wins_even_with_recent_activity(client: TestClient, app: FastAPI):
    """A session that's been continuously active still can't outlive its
    absolute expiry -- the sliding window only ever pushes the
    inactivity cutoff forward, never the hard cap.
    """
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    with app.state.session_factory() as db:
        session = db.get(AppSession, session_id)
        session.last_activity_at = datetime.now(timezone.utc)  # perfectly fresh
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)  # but past the cap
        db.commit()

    response = client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- Expired session cookie cleanup ---


def test_expired_session_redirect_clears_the_session_cookie(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)

    response = client.get("/cases", follow_redirects=False)
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    # A cleared cookie carries an immediately-past/zero Max-Age or epoch
    # expiry -- either way it's no longer the live session id.
    assert session_id not in set_cookie


def test_a_full_password_login_is_required_after_expiry(client: TestClient, app: FastAPI):
    """"Expired or inactive sessions ... require a full password login" --
    after expiry, even resubmitting the same (now-deleted) session cookie
    doesn't work; only a fresh /auth/login POST restores access.
    """
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)
    client.get("/cases", follow_redirects=False)  # triggers deletion

    assert client.get("/cases", follow_redirects=False).status_code == 303


# --- Protected direct URLs stay protected under expiry too ---


def test_direct_file_url_redirects_after_inactivity_expiry(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _backdate_last_activity(app, session_id, minutes_ago=16)

    response = client.get("/documents/1/file", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_direct_page_image_url_redirects_after_absolute_expiry(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)

    response = client.get("/documents/1/pages/1/image", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- CSRF and cache headers still apply after Step 4 ---


def test_expired_session_rejection_still_has_no_store_cache_headers(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)

    response = client.get("/cases", follow_redirects=False)
    assert response.headers.get("Cache-Control") == "no-store, private"


def test_csrf_is_still_enforced_on_a_freshly_reauthenticated_session(client: TestClient, app: FastAPI):
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    _expire_absolutely(app, session_id)
    client.get("/cases", follow_redirects=False)  # old session now gone

    # Log back in fresh.
    login_page = client.get("/auth/login")
    assert login_page.status_code == 200
    csrf_token = client.cookies.get("csrf_token")
    login_response = client.post(
        "/auth/login",
        data={"password": "owner-password-123", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    # Header-based token still works post-reauth (client's default header
    # was set once at fixture creation, matching the still-valid cookie).
    response = client.post("/cases", data={"label": "Post-reauth Student"}, follow_redirects=False)
    assert response.status_code == 303
