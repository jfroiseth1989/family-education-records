"""Double-submit-cookie CSRF protection (Security Phase Steps 2 and 3.5).

Works before login exists -- unlike app_sessions, a CSRF token needs no
server-side state: the same random value is set as a cookie and
rendered into the form as a hidden field; a POST is accepted only if
the two match. A cross-site page can trigger a request to this
application's origin (that's what CSRF protects against), but it cannot
read or set this application's cookies from another origin, so it can
never produce a matching pair.

Step 2 wired this into the auth forms (setup, login, lock, logout) via
explicit per-route `csrf_token: str = Form(...)` parameters and
`verify_csrf()` calls. Step 3.5 protects every other existing
state-changing route the same way, but centrally: rather than retrofitting
~24 route handlers, `AuthEnforcementMiddleware`
(app/core/auth/enforcement.py) calls `extract_submitted_csrf_token()` and
`csrf_token_matches()` below for every mutating request outside `/auth/*`.
The auth routes are untouched by that middleware check (they're inside
the always-public `/auth/*` prefix) and keep doing their own explicit
verification exactly as before.

Non-browser clients (scripts, curl, anything that isn't rendering this
app's HTML forms): fetch any page first to receive the `csrf_token`
cookie (e.g. `GET /cases`, which -- once logged in -- always carries it,
since it's set no later than the `/auth/login` or `/auth/setup` page that
necessarily preceded it), then send that same value back on every
mutating request as the `X-CSRF-Token` header. The header is checked
first specifically so a scripted client never has to multipart-encode a
`csrf_token` field alongside a file upload just to satisfy this check --
see `extract_submitted_csrf_token()`.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
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


def csrf_token_matches(request: Request, submitted_token: str | None) -> bool:
    """True iff `submitted_token` matches this request's CSRF cookie
    exactly, compared in constant time via `secrets.compare_digest`
    (never a plain `==` string comparison). False (not an exception) for
    a missing cookie or missing/empty submitted token -- the two callers
    each turn that into the response shape that fits their layer:
    `verify_csrf()` raises for route handlers, `AuthEnforcementMiddleware`
    returns a 403 `Response` directly (raising from inside
    `BaseHTTPMiddleware.dispatch()`, rather than from `call_next()`,
    would skip FastAPI's exception handlers and surface as an unhandled
    500 instead -- see that middleware's docstring).
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        return False
    return secrets.compare_digest(cookie_token, submitted_token)


def verify_csrf(request: Request, submitted_token: str) -> None:
    """Raise HTTPException(403) unless `submitted_token` matches this
    request's CSRF cookie (see `csrf_token_matches()`).

    Call at the very top of every POST handler this token protects,
    before any other side effect (including any database write). Used
    directly only by the `/auth/*` routes, which parse their own
    `csrf_token: str = Form(...)` field; every other state-changing route
    is instead protected centrally by `AuthEnforcementMiddleware`.
    """
    if not csrf_token_matches(request, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


async def extract_submitted_csrf_token(request: Request) -> str | None:
    """Pull the caller's submitted CSRF token from `request`, or None if
    it supplied neither form of it.

    Checked in this order:

    1. The `X-CSRF-Token` header -- the path for non-browser/API clients
       (see this module's docstring). Checked first and returned
       immediately so a client using it never pays the cost of the body
       being read at all, and so a large multipart file upload doesn't
       need a `csrf_token` field stitched into its encoding just to pass
       this check.
    2. A `csrf_token` form field -- the path every HTML form in this
       application uses (a hidden input alongside the cookie, the
       standard double-submit pattern).

    Reads `request.body()` before `request.form()` so the raw bytes are
    cached on this `Request` object first; `BaseHTTPMiddleware.call_next()`
    hands the *same* Request instance's `.receive` through to the route
    handler below, and its `.stream()` (which both `.body()` and `.form()`
    are built on) replays the cached bytes instead of trying to read the
    already-drained ASGI receive channel a second time. Calling
    `.form()` directly, without `.body()` first, does not cache reusable
    bytes the same way and silently leaves the downstream route's own
    `Form(...)` parameters empty -- verified empirically before relying
    on this, not assumed.
    """
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return header_token

    await request.body()
    form = await request.form()
    try:
        token = form.get("csrf_token")
    finally:
        await form.close()
    return str(token) if token is not None else None
