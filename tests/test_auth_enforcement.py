"""Tests for the deny-by-default authentication enforcement middleware
and cache-control protections (Security Phase Step 3).

Uses `anonymous_client` for the denial-path assertions and `client` (a
pre-authenticated client -- see tests/conftest.py) for the allow-path
assertions.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


# --- Deny-by-default: representative existing routes ---


def test_get_cases_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_post_cases_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.post(
        "/cases", data={"label": "Should Not Be Created"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_get_root_redirects_through_to_login_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/", follow_redirects=False)
    # "/" itself 303s to "/cases", which the enforcement middleware then
    # also blocks -- following just the one hop is enough to prove "/" is
    # not itself an accidental public path.
    assert response.status_code == 303


def test_openapi_docs_are_not_reachable_when_unauthenticated(anonymous_client: TestClient):
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = anonymous_client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/auth/login"


# --- Direct document/page-image URL protection ---


def test_document_file_download_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/documents/1/file", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_document_page_image_redirects_when_unauthenticated(anonymous_client: TestClient):
    response = anonymous_client.get("/documents/1/pages/1/image", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_document_file_download_is_reachable_with_a_valid_session(client: TestClient):
    """A valid session should reach the real route (and its own 404 for a
    document that doesn't exist), not the enforcement redirect -- proves
    the middleware is passing authenticated requests through rather than
    coincidentally matching everything.
    """
    response = client.get("/documents/999999/file", follow_redirects=False)
    assert response.status_code == 404


# --- Allow-list: /auth/* and /static/* stay public ---


def test_auth_login_page_is_reachable_without_a_session(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/login")
    assert response.status_code == 200


def test_auth_setup_page_is_reachable_without_a_session(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/setup")
    assert response.status_code == 200


def test_static_asset_is_reachable_without_a_session(anonymous_client: TestClient):
    response = anonymous_client.get("/static/style.css")
    assert response.status_code == 200


# --- Session validity edge cases ---


def test_unknown_session_cookie_is_treated_as_unauthenticated(anonymous_client: TestClient):
    anonymous_client.cookies.set("ferchronos_session", "not-a-real-session-id")
    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# --- Cache-control headers ---


def test_protected_response_has_no_store_cache_headers(client: TestClient):
    response = client.get("/cases")
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Pragma"] == "no-cache"


def test_login_redirect_response_has_no_store_cache_headers(anonymous_client: TestClient):
    response = anonymous_client.get("/cases", follow_redirects=False)
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Pragma"] == "no-cache"


def test_auth_page_response_has_no_store_cache_headers(anonymous_client: TestClient):
    response = anonymous_client.get("/auth/login")
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Pragma"] == "no-cache"


def test_document_file_response_has_no_store_cache_headers(client: TestClient):
    response = client.get("/documents/999999/file")
    assert response.headers.get("Cache-Control") == "no-store, private"


def test_static_asset_is_not_marked_no_store(anonymous_client: TestClient):
    response = anonymous_client.get("/static/style.css")
    assert response.headers.get("Cache-Control") != "no-store, private"
