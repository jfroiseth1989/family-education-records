"""End-to-end tests for first-run setup, login, lock, and logout
(Security Phase Step 2), plus deny-by-default enforcement (Step 3), via
the FastAPI TestClient.

Uses `anonymous_client` throughout: this file is specifically about the
pre-authenticated flows (setup/login/lock/logout) themselves, so it needs
a pristine client with no account and no session, not the suite-wide
`client` fixture (which completes setup before yielding -- see
tests/conftest.py).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth.passwords import verify_password
from app.core.auth.session import SESSION_COOKIE_NAME
from app.db.models import AppAuth, AppSession


def _csrf_token(client: TestClient) -> str:
    """Fetch a page that sets the CSRF cookie, then return its value --
    tests submit this same value back as the form field.
    """
    client.get("/auth/setup")
    return client.cookies.get("csrf_token")


def _complete_setup(client: TestClient, password: str = "owner-password-123") -> None:
    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/setup",
        data={"password": password, "confirm_password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- First-run setup ---


def test_get_setup_shows_form_when_no_account_exists(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/setup")
    assert response.status_code == 200
    assert "Create password" in response.text


def test_get_setup_redirects_to_login_once_account_exists(anonymous_client: TestClient):
    _complete_setup(anonymous_client)
    response = anonymous_client.get("/auth/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_post_setup_creates_account_and_session_cookie(anonymous_client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/setup",
        data={
            "password": "owner-password-123",
            "confirm_password": "owner-password-123",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/cases"
    assert SESSION_COOKIE_NAME in response.cookies

    with app.state.session_factory() as db:
        auth_rows = db.scalars(select(AppAuth)).all()
        session_rows = db.scalars(select(AppSession)).all()
    assert len(auth_rows) == 1
    assert len(session_rows) == 1
    # Never the plaintext password.
    assert "owner-password-123" not in auth_rows[0].password_hash


def test_post_setup_rejects_mismatched_passwords(anonymous_client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/setup",
        data={"password": "owner-password-123", "confirm_password": "different", "csrf_token": csrf_token},
    )
    assert response.status_code == 400
    assert "do not match" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_rejects_short_password(anonymous_client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/setup",
        data={"password": "short", "confirm_password": "short", "csrf_token": csrf_token},
    )
    assert response.status_code == 400
    assert "at least" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_without_csrf_token_is_rejected(anonymous_client: TestClient, app: FastAPI):
    anonymous_client.get("/auth/setup")  # sets the csrf cookie, but we deliberately don't use it
    response = anonymous_client.post(
        "/auth/setup",
        data={"password": "owner-password-123", "confirm_password": "owner-password-123", "csrf_token": "wrong-value"},
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_refuses_to_run_twice(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client, password="first-owner-password")

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/setup",
        data={
            "password": "second-owner-password",
            "confirm_password": "second-owner-password",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"

    with app.state.session_factory() as db:
        auth_rows = db.scalars(select(AppAuth)).all()
    assert len(auth_rows) == 1
    # The original password is still the one that verifies -- never
    # silently overwritten.
    assert verify_password("first-owner-password", auth_rows[0].password_hash)


# --- Login ---


def test_get_login_redirects_to_setup_when_no_account_exists(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/setup"


def test_get_login_shows_form_once_account_exists(anonymous_client: TestClient):
    _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()
    response = anonymous_client.get("/auth/login")
    assert response.status_code == 200
    assert "Log in" in response.text


def test_post_login_with_correct_password_succeeds(anonymous_client: TestClient):
    _complete_setup(anonymous_client, password="owner-password-123")
    anonymous_client.cookies.clear()  # start fresh, as a new visitor would

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/login",
        data={"password": "owner-password-123", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/cases"
    assert SESSION_COOKIE_NAME in response.cookies


def test_post_login_with_wrong_password_shows_generic_error(anonymous_client: TestClient):
    _complete_setup(anonymous_client, password="owner-password-123")
    anonymous_client.cookies.clear()

    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/login",
        data={"password": "totally-wrong", "csrf_token": csrf_token},
    )
    assert response.status_code == 401
    assert "Incorrect password." in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_post_login_without_csrf_token_is_rejected(anonymous_client: TestClient):
    _complete_setup(anonymous_client, password="owner-password-123")
    anonymous_client.cookies.clear()
    anonymous_client.get("/auth/login")

    response = anonymous_client.post(
        "/auth/login", data={"password": "owner-password-123", "csrf_token": "wrong-value"}
    )
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in response.cookies


# --- Lock / logout ---


def test_post_lock_ends_session_and_redirects_with_locked_flag(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client, password="owner-password-123")
    session_id_before = anonymous_client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id_before is not None

    csrf_token = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/lock", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?locked=1"

    with app.state.session_factory() as db:
        assert db.scalars(select(AppSession)).all() == []


def test_post_logout_ends_session_and_redirects_without_locked_flag(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client, password="owner-password-123")

    csrf_token = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"

    with app.state.session_factory() as db:
        assert db.scalars(select(AppSession)).all() == []


def test_locked_login_page_shows_locked_copy(anonymous_client: TestClient):
    _complete_setup(anonymous_client)
    response = anonymous_client.get("/auth/login?locked=1")
    assert "locked" in response.text.lower()


# --- Step 3: deny-by-default enforcement ---


def test_protected_route_redirects_to_login_with_no_session(anonymous_client: TestClient):
    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_protected_route_redirects_to_login_after_account_exists_but_before_login(
    anonymous_client: TestClient,
):
    _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()  # no session at all now

    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_protected_route_is_reachable_with_a_valid_session(client: TestClient):
    response = client.get("/cases")
    assert response.status_code == 200


def test_protected_route_redirects_after_logout(anonymous_client: TestClient):
    _complete_setup(anonymous_client)
    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})

    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- No data leakage on auth pages ---


def test_login_page_never_shows_the_student_selector_or_student_names(client: TestClient):
    create_response = client.post("/cases", data={"label": "Secret Student Name"})
    assert create_response.status_code in (200, 303)

    csrf_token = client.cookies.get("csrf_token")
    client.post("/auth/logout", data={"csrf_token": csrf_token})

    response = client.get("/auth/login")
    assert "student-selector" not in response.text
    assert "Secret Student Name" not in response.text


def test_setup_page_never_shows_the_student_selector(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/setup")
    assert "student-selector" not in response.text


# --- Header reflects session state ---


def test_login_page_itself_has_no_account_area_header(anonymous_client: TestClient):
    """auth_base.html deliberately doesn't extend base.html, so the login
    page itself never shows the Lock/Log out/Log in account-area -- see
    templates/auth_base.html.
    """
    response = anonymous_client.get("/auth/login")
    assert "account-area" not in response.text


def test_header_shows_lock_and_logout_when_a_valid_session_exists(client: TestClient):
    response = client.get("/cases")
    assert ">Lock<" in response.text
    assert ">Log out<" in response.text
