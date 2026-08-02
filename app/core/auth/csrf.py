"""Double-submit-cookie CSRF protection (Security Phase Step 2).

Works before login exists -- unlike app_sessions, a CSRF token needs no
server-side state: the same random value is set as a cookie and
rendered into the form as a hidden field; a POST is accepted only if
the two match. A cross-site page can trigger a request to this
application's origin (that's what CSRF protects against), but it cannot
read or set this application's cookies from another origin, so it can
never produce a matching pair.

Every new state-changing route this application adds from this step
forward must call `verify_csrf()`, per the Security Phase plan; wiring
CSRF into routes that already existed before this phase is separate,
later work -- this step covers exactly the auth forms it introduces
(setup, login, lock, logout).
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response

CSRF_COOKIE_NAME = "csrf_token"
_TOKEN_BYTES = 32


def get_or_create_csrf_token(request: Request) -> str:
    """Return this browser's CSRF token, generating a new one if absent.

    Deliberately does not set the cookie itself -- Starlette's
    `TemplateResponse` renders its body at construction time, so the
    token value must be known and passed into the template context
    *before* the response object exists; `set_csrf_cookie()` then
    attaches it to that already-built response. Call both, in that
    order, from every GET route that renders a form this token protects.
    """
    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(_TOKEN_BYTES)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Attach `token` (from `get_or_create_csrf_token`) to `response` as
    the CSRF cookie. Safe to call even when the token was reused from an
    existing cookie -- re-setting it to the same value is a no-op in
    effect.
    """
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        # Loopback-only plain HTTP by design, no TLS layer -- see
        # docs/PRIVACY_SECURITY.md §2. `Secure` would add nothing here.
        secure=False,
    )


def verify_csrf(request: Request, submitted_token: str) -> None:
    """Raise HTTPException(403) unless `submitted_token` matches this
    request's CSRF cookie exactly, compared in constant time via
    `secrets.compare_digest` (never a plain `==` string comparison).

    Call at the very top of every POST handler this token protects,
    before any other side effect (including any database write).
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
    if not secrets.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
