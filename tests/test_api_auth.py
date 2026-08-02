"""End-to-end tests for first-run setup, login, lock, and logout
(Security Phase Step 2), via the FastAPI TestClient.

Deliberately includes regression checks proving every pre-existing route
stays fully open in this step -- enforcement is a separate, later step.
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


def test_get_setup_shows_form_when_no_account_exists(client: TestClient):
    response = client.get("/auth/setup")
    assert response.status_code == 200
    assert "Create password" in response.text


def test_get_setup_redirects_to_login_once_account_exists(client: TestClient):
    _complete_setup(client)
    response = client.get("/auth/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_post_setup_creates_account_and_session_cookie(client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(client)
    response = client.post(
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


def test_post_setup_rejects_mismatched_passwords(client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/setup",
        data={"password": "owner-password-123", "confirm_password": "different", "csrf_token": csrf_token},
    )
    assert response.status_code == 400
    assert "do not match" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_rejects_short_password(client: TestClient, app: FastAPI):
    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/setup",
        data={"password": "short", "confirm_password": "short", "csrf_token": csrf_token},
    )
    assert response.status_code == 400
    assert "at least" in response.text.lower()

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_without_csrf_token_is_rejected(client: TestClient, app: FastAPI):
    client.get("/auth/setup")  # sets the csrf cookie, but we deliberately don't use it
    response = client.post(
        "/auth/setup",
        data={"password": "owner-password-123", "confirm_password": "owner-password-123", "csrf_token": "wrong-value"},
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(AppAuth)).all() == []


def test_post_setup_refuses_to_run_twice(client: TestClient, app: FastAPI):
    _complete_setup(client, password="first-owner-password")

    csrf_token = _csrf_token(client)
    response = client.post(
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


def test_get_login_redirects_to_setup_when_no_account_exists(client: TestClient):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/setup"


def test_get_login_shows_form_once_account_exists(client: TestClient):
    _complete_setup(client)
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert "Log in" in response.text


def test_post_login_with_correct_password_succeeds(client: TestClient):
    _complete_setup(client, password="owner-password-123")
    client.cookies.clear()  # start fresh, as a new visitor would

    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/login",
        data={"password": "owner-password-123", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/cases"
    assert SESSION_COOKIE_NAME in response.cookies


def test_post_login_with_wrong_password_shows_generic_error(client: TestClient):
    _complete_setup(client, password="owner-password-123")
    client.cookies.clear()

    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/login",
        data={"password": "totally-wrong", "csrf_token": csrf_token},
    )
    assert response.status_code == 401
    assert "Incorrect password." in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_post_login_without_csrf_token_is_rejected(client: TestClient):
    _complete_setup(client, password="owner-password-123")
    client.cookies.clear()
    client.get("/auth/login")

    response = client.post(
        "/auth/login", data={"password": "owner-password-123", "csrf_token": "wrong-value"}
    )
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in response.cookies


# --- Lock / logout ---


def test_post_lock_ends_session_and_redirects_with_locked_flag(client: TestClient, app: FastAPI):
    _complete_setup(client, password="owner-password-123")
    session_id_before = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id_before is not None

    csrf_token = client.cookies.get("csrf_token")
    response = client.post(
        "/auth/lock", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?locked=1"

    with app.state.session_factory() as db:
        assert db.scalars(select(AppSession)).all() == []


def test_post_logout_ends_session_and_redirects_without_locked_flag(client: TestClient, app: FastAPI):
    _complete_setup(client, password="owner-password-123")

    csrf_token = client.cookies.get("csrf_token")
    response = client.post(
        "/auth/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"

    with app.state.session_factory() as db:
        assert db.scalars(select(AppSession)).all() == []


def test_locked_login_page_shows_locked_copy(client: TestClient):
    _complete_setup(client)
    response = client.get("/auth/login?locked=1")
    assert "locked" in response.text.lower()


# --- Step 2 must not enforce anything yet ---


def test_existing_routes_remain_fully_open_with_no_session(client: TestClient):
    """The whole point of this step: setup/login/lock/logout exist and
    work, but nothing else in the application checks for them yet.
    """
    response = client.get("/cases")
    assert response.status_code == 200


def test_existing_routes_remain_open_even_after_an_account_is_created(client: TestClient):
    _complete_setup(client)
    client.cookies.clear()  # no session at all now

    response = client.get("/cases")
    assert response.status_code == 200


# --- No data leakage on auth pages ---


def test_login_page_never_shows_the_student_selector_or_student_names(client: TestClient):
    create_response = client.post("/cases", data={"label": "Secret Student Name"})
    assert create_response.status_code in (200, 303)

    response = client.get("/auth/login")
    assert "student-selector" not in response.text
    assert "Secret Student Name" not in response.text


def test_setup_page_never_shows_the_student_selector(client: TestClient):
    response = client.get("/auth/setup")
    assert "student-selector" not in response.text


# --- Header reflects session state ---


def test_header_shows_log_in_link_when_no_session(client: TestClient):
    response = client.get("/cases")
    assert 'href="/auth/login"' in response.text
    assert ">Lock<" not in response.text


def test_header_shows_lock_and_logout_when_a_valid_session_exists(client: TestClient):
    _complete_setup(client)
    response = client.get("/cases")
    assert ">Lock<" in response.text
    assert ">Log out<" in response.text
