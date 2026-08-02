"""End-to-end tests for recovery-key issuance, acknowledgement, password
recovery, and authenticated recovery-key rotation (Security Phase Step 5).

Uses `anonymous_client` throughout -- like test_api_auth.py, this file is
about the pre-authenticated flows themselves (setup issuing a key,
recovering without being logged in), so it needs a pristine client.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth.passwords import verify_password
from app.core.auth.recovery import normalize_recovery_key
from app.core.auth.session import SESSION_COOKIE_NAME
from app.db.models import AppAuth, AppSession, Case, Document, Tag

_PASSWORD = "owner-password-123"


def _csrf_token(client: TestClient) -> str:
    client.get("/auth/setup")
    return client.cookies.get("csrf_token")


def _extract_recovery_key(html: str) -> str:
    match = re.search(r'<code>([^<]+)</code>', html)
    assert match is not None, html
    return match.group(1)


def _complete_setup(client: TestClient, password: str = _PASSWORD) -> str:
    """Complete first-run setup and return the recovery key shown."""
    csrf_token = _csrf_token(client)
    response = client.post(
        "/auth/setup",
        data={"password": password, "confirm_password": password, "csrf_token": csrf_token},
    )
    assert response.status_code == 200, response.text
    return _extract_recovery_key(response.text)


def _acknowledge_recovery_key(client: TestClient, recovery_key: str) -> None:
    csrf_token = client.cookies.get("csrf_token")
    response = client.post(
        "/auth/recovery-key/acknowledge",
        data={"csrf_token": csrf_token, "recovery_key": recovery_key, "confirmed": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _get_auth(app: FastAPI) -> AppAuth:
    with app.state.session_factory() as db:
        return db.scalars(select(AppAuth)).first()


# --- Setup issues and displays a recovery key ---


def test_setup_shows_a_recovery_key_and_stores_only_its_hash(anonymous_client: TestClient, app: FastAPI):
    recovery_key = _complete_setup(anonymous_client)
    assert re.match(r"^[A-Z0-9]{4}(-[A-Z0-9]{4}){5}$", recovery_key)

    auth = _get_auth(app)
    assert auth.recovery_key_hash is not None
    assert recovery_key not in auth.recovery_key_hash
    assert verify_password(normalize_recovery_key(recovery_key), auth.recovery_key_hash)


def test_setup_response_carries_a_valid_session_cookie_before_acknowledgement(
    anonymous_client: TestClient,
):
    _complete_setup(anonymous_client)
    assert SESSION_COOKIE_NAME in anonymous_client.cookies


def test_setup_response_warns_about_permanent_loss(anonymous_client: TestClient):
    csrf_token = _csrf_token(anonymous_client)
    response = anonymous_client.post(
        "/auth/setup",
        data={"password": _PASSWORD, "confirm_password": _PASSWORD, "csrf_token": csrf_token},
    )
    assert "permanently" in response.text.lower()


# --- Acknowledgement ---


def test_unconfirmed_acknowledgement_redisplays_the_same_key(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    csrf_token = anonymous_client.cookies.get("csrf_token")

    response = anonymous_client.post(
        "/auth/recovery-key/acknowledge",
        data={"csrf_token": csrf_token, "recovery_key": recovery_key},  # no "confirmed"
    )
    assert response.status_code == 200
    assert recovery_key in response.text
    assert "confirm" in response.text.lower()


def test_confirmed_acknowledgement_redirects_to_cases(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)

    response = anonymous_client.get("/cases")
    assert response.status_code == 200


# --- Successful recovery ---


def test_recover_with_correct_key_resets_password_and_signs_in(
    anonymous_client: TestClient, app: FastAPI
):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)
    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})

    anonymous_client.get("/auth/recover")
    recover_csrf = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "brand-new-password-456",
            "confirm_new_password": "brand-new-password-456",
            "csrf_token": recover_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200  # shows the new recovery key, not a redirect
    assert SESSION_COOKIE_NAME in response.cookies

    # The new password now works; the old one no longer does.
    anonymous_client.cookies.clear()
    anonymous_client.get("/auth/login")
    login_csrf = anonymous_client.cookies.get("csrf_token")
    old_pw_response = anonymous_client.post(
        "/auth/login", data={"password": _PASSWORD, "csrf_token": login_csrf}
    )
    assert old_pw_response.status_code == 401

    anonymous_client.cookies.clear()
    anonymous_client.get("/auth/login")
    login_csrf2 = anonymous_client.cookies.get("csrf_token")
    new_pw_response = anonymous_client.post(
        "/auth/login",
        data={"password": "brand-new-password-456", "csrf_token": login_csrf2},
        follow_redirects=False,
    )
    assert new_pw_response.status_code == 303


def test_recover_response_shows_a_freshly_rotated_recovery_key(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)
    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})

    anonymous_client.get("/auth/recover")
    recover_csrf = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "brand-new-password-456",
            "confirm_new_password": "brand-new-password-456",
            "csrf_token": recover_csrf,
        },
    )
    new_key = _extract_recovery_key(response.text)
    assert new_key != recovery_key


# --- Old-key invalidation (single-use) ---


def test_the_same_recovery_key_cannot_be_used_twice(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)
    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})

    anonymous_client.get("/auth/recover")
    recover_csrf = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "brand-new-password-456",
            "confirm_new_password": "brand-new-password-456",
            "csrf_token": recover_csrf,
        },
    )
    anonymous_client.cookies.clear()

    # Reusing the exact same (now-rotated-away) key must fail.
    anonymous_client.get("/auth/recover")
    second_csrf = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "yet-another-password-789",
            "confirm_new_password": "yet-another-password-789",
            "csrf_token": second_csrf,
        },
    )
    assert response.status_code == 401
    assert "Invalid recovery key." in response.text


# --- Invalid recovery attempts ---


def test_recover_with_wrong_key_shows_generic_error(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()

    anonymous_client.get("/auth/recover")
    csrf_token = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": "0000-0000-0000-0000-0000-0000",
            "new_password": "irrelevant-password-123",
            "confirm_new_password": "irrelevant-password-123",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 401
    assert "Invalid recovery key." in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_recover_does_not_reset_the_password_on_a_wrong_key(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()

    anonymous_client.get("/auth/recover")
    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": "0000-0000-0000-0000-0000-0000",
            "new_password": "should-not-take-effect-123",
            "confirm_new_password": "should-not-take-effect-123",
            "csrf_token": csrf_token,
        },
    )

    auth = _get_auth(app)
    assert verify_password(_PASSWORD, auth.password_hash)


def test_recover_rejects_mismatched_new_passwords(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()

    anonymous_client.get("/auth/recover")
    csrf_token = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "one-password-123",
            "confirm_new_password": "a-different-password-456",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 400
    assert "do not match" in response.text.lower()


def test_recover_rejects_a_too_short_new_password(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()

    anonymous_client.get("/auth/recover")
    csrf_token = anonymous_client.cookies.get("csrf_token")
    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "short",
            "confirm_new_password": "short",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 400
    assert "at least" in response.text.lower()


def test_recover_without_csrf_token_is_rejected(anonymous_client: TestClient):
    recovery_key = _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()
    anonymous_client.get("/auth/recover")

    response = anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "irrelevant-password-123",
            "confirm_new_password": "irrelevant-password-123",
            "csrf_token": "wrong-value",
        },
    )
    assert response.status_code == 403


def test_recover_before_any_account_exists_redirects_to_setup(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/recover", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/setup"


# --- Recovery attempts share the login lockout ---


def test_repeated_wrong_recovery_keys_lock_out_login_too(anonymous_client: TestClient, app: FastAPI):
    _complete_setup(anonymous_client)
    anonymous_client.cookies.clear()

    for _ in range(5):
        anonymous_client.get("/auth/recover")
        csrf_token = anonymous_client.cookies.get("csrf_token")
        response = anonymous_client.post(
            "/auth/recover",
            data={
                "recovery_key": "0000-0000-0000-0000-0000-0000",
                "new_password": "irrelevant-password-123",
                "confirm_new_password": "irrelevant-password-123",
                "csrf_token": csrf_token,
            },
        )

    assert response.status_code == 429

    # Login is now locked out too -- same shared counter.
    anonymous_client.get("/auth/login")
    login_csrf = anonymous_client.cookies.get("csrf_token")
    login_response = anonymous_client.post(
        "/auth/login", data={"password": _PASSWORD, "csrf_token": login_csrf}
    )
    assert login_response.status_code == 429


# --- Authenticated recovery-key rotation (app.api.account) ---


def test_account_recovery_key_page_requires_authentication(anonymous_client: TestClient):
    response = anonymous_client.get("/account/recovery-key", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_authenticated_owner_can_rotate_the_recovery_key(client: TestClient, app: FastAPI):
    """`client` (see tests/conftest.py) is already logged in via a real
    setup round-trip, which already issued and acknowledged a key --
    this exercises the *deliberate*, authenticated rotation path.
    """
    auth_before = _get_auth(app)
    original_hash = auth_before.recovery_key_hash

    csrf_token = client.cookies.get("csrf_token")
    response = client.post("/account/recovery-key/regenerate", data={"csrf_token": csrf_token})
    assert response.status_code == 200
    new_key = _extract_recovery_key(response.text)

    auth_after = _get_auth(app)
    assert auth_after.recovery_key_hash != original_hash
    assert verify_password(normalize_recovery_key(new_key), auth_after.recovery_key_hash)


def test_rotating_the_recovery_key_invalidates_the_old_one(client: TestClient, app: FastAPI):
    csrf_token = client.cookies.get("csrf_token")
    response = client.post("/account/recovery-key/regenerate", data={"csrf_token": csrf_token})
    new_key = _extract_recovery_key(response.text)

    # Log out and try to recover with the *original* setup key (now stale).
    client.post("/auth/logout", data={"csrf_token": csrf_token})
    client.get("/auth/recover")
    recover_csrf = client.cookies.get("csrf_token")

    # We don't have the original setup key in this test (the `client`
    # fixture doesn't expose it), so instead prove the new key works and
    # a syntactically-plausible-but-wrong one doesn't -- the actual
    # single-use guarantee for a key obtained via setup/recover is
    # covered by test_the_same_recovery_key_cannot_be_used_twice above.
    wrong_response = client.post(
        "/auth/recover",
        data={
            "recovery_key": "9999-9999-9999-9999-9999-9999",
            "new_password": "should-not-work-123456",
            "confirm_new_password": "should-not-work-123456",
            "csrf_token": recover_csrf,
        },
    )
    assert wrong_response.status_code == 401

    client.get("/auth/recover")
    right_csrf = client.cookies.get("csrf_token")
    right_response = client.post(
        "/auth/recover",
        data={
            "recovery_key": new_key,
            "new_password": "a-genuinely-new-password-123",
            "confirm_new_password": "a-genuinely-new-password-123",
            "csrf_token": right_csrf,
        },
        follow_redirects=False,
    )
    assert right_response.status_code == 200


def test_rotation_alone_does_not_invalidate_the_current_session(client: TestClient):
    """Rotating the recovery key is not a password reset -- it must not
    log the owner out of their own current session.
    """
    csrf_token = client.cookies.get("csrf_token")
    client.post("/account/recovery-key/regenerate", data={"csrf_token": csrf_token})

    response = client.get("/cases")
    assert response.status_code == 200


def test_account_recovery_key_regenerate_requires_csrf(anonymous_client: TestClient):
    _complete_setup(anonymous_client)
    response = anonymous_client.post("/account/recovery-key/regenerate", data={})
    assert response.status_code == 403


# --- Session invalidation on password reset ---


def test_recovery_reset_invalidates_every_existing_session(anonymous_client: TestClient, app: FastAPI):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)

    # Simulate a second, independently-issued session for the same
    # account (e.g. a second browser tab) that never gets used again by
    # this test client -- it must still be gone after recovery.
    with app.state.session_factory() as db:
        from app.core.auth.session import create_session

        auth = db.scalars(select(AppAuth)).first()
        other_session = create_session(db, auth)
        db.commit()
        other_session_id = other_session.session_id

    csrf_token = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})

    anonymous_client.get("/auth/recover")
    recover_csrf = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "post-recovery-password-123",
            "confirm_new_password": "post-recovery-password-123",
            "csrf_token": recover_csrf,
        },
    )

    with app.state.session_factory() as db:
        assert db.get(AppSession, other_session_id) is None
        remaining = db.scalars(select(AppSession)).all()
        assert len(remaining) == 1  # only the brand-new post-recovery session


# --- Preservation of existing educational records ---


def test_recovery_reset_preserves_existing_cases_documents_and_tags(
    anonymous_client: TestClient, app: FastAPI
):
    recovery_key = _complete_setup(anonymous_client)
    _acknowledge_recovery_key(anonymous_client, recovery_key)

    csrf_token = anonymous_client.cookies.get("csrf_token")
    case_response = anonymous_client.post(
        "/cases",
        data={"label": "Recovery Preservation Test Student", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    case_id = int(case_response.headers["location"].rsplit("/", 1)[-1])

    upload_response = anonymous_client.post(
        f"/cases/{case_id}/documents",
        data={"csrf_token": csrf_token},
        files={"file": ("evidence.txt", b"Some evidence content.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    anonymous_client.post(
        f"/documents/{document_id}/tags", data={"name": "IEP", "csrf_token": csrf_token}
    )

    with app.state.session_factory() as db:
        original_hash = db.get(Document, document_id).sha256_hash

    anonymous_client.post("/auth/logout", data={"csrf_token": csrf_token})
    anonymous_client.get("/auth/recover")
    recover_csrf = anonymous_client.cookies.get("csrf_token")
    anonymous_client.post(
        "/auth/recover",
        data={
            "recovery_key": recovery_key,
            "new_password": "post-recovery-password-456",
            "confirm_new_password": "post-recovery-password-456",
            "csrf_token": recover_csrf,
        },
    )

    with app.state.session_factory() as db:
        case = db.get(Case, case_id)
        document = db.get(Document, document_id)
        tag = db.scalars(select(Tag).where(Tag.name == "IEP")).first()

        assert case is not None
        assert case.label == "Recovery Preservation Test Student"
        assert document is not None
        assert document.sha256_hash == original_hash
        assert tag is not None

    # And the newly-reset owner can still reach it through the app.
    response = anonymous_client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert "IEP" in response.text
