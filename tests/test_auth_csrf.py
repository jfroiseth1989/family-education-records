"""Tests for app/core/auth/csrf.py -- double-submit-cookie CSRF
protection (Security Phase Step 2).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from app.core.auth.csrf import CSRF_COOKIE_NAME, get_or_create_csrf_token, set_csrf_cookie, verify_csrf


class _FakeRequest:
    """Minimal stand-in for fastapi.Request -- these functions only ever
    read `.cookies`, so a full Starlette Request isn't needed here.
    """

    def __init__(self, cookies: dict[str, str] | None = None):
        self.cookies = cookies or {}


def test_get_or_create_csrf_token_generates_a_token_when_absent():
    token = get_or_create_csrf_token(_FakeRequest())
    assert token
    assert len(token) > 20


def test_get_or_create_csrf_token_reuses_existing_cookie():
    request = _FakeRequest({CSRF_COOKIE_NAME: "existing-token-value"})
    assert get_or_create_csrf_token(request) == "existing-token-value"


def test_get_or_create_csrf_token_is_random_across_calls_without_a_cookie():
    first = get_or_create_csrf_token(_FakeRequest())
    second = get_or_create_csrf_token(_FakeRequest())
    assert first != second


def test_set_csrf_cookie_sets_the_expected_cookie_attributes():
    response = Response()
    set_csrf_cookie(response, "some-token")

    set_cookie_header = response.headers.get("set-cookie")
    assert set_cookie_header is not None
    assert "csrf_token=some-token" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert "samesite=strict" in set_cookie_header.lower()


def test_verify_csrf_accepts_matching_token():
    request = _FakeRequest({CSRF_COOKIE_NAME: "matching-value"})
    # Must not raise.
    verify_csrf(request, "matching-value")


def test_verify_csrf_rejects_missing_cookie():
    request = _FakeRequest({})
    with pytest.raises(HTTPException) as exc_info:
        verify_csrf(request, "some-submitted-value")
    assert exc_info.value.status_code == 403


def test_verify_csrf_rejects_missing_submitted_token():
    request = _FakeRequest({CSRF_COOKIE_NAME: "cookie-value"})
    with pytest.raises(HTTPException) as exc_info:
        verify_csrf(request, "")
    assert exc_info.value.status_code == 403


def test_verify_csrf_rejects_mismatched_values():
    request = _FakeRequest({CSRF_COOKIE_NAME: "cookie-value"})
    with pytest.raises(HTTPException) as exc_info:
        verify_csrf(request, "a-different-value")
    assert exc_info.value.status_code == 403
