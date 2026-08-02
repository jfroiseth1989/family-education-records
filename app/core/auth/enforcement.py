"""Deny-by-default authentication + CSRF enforcement (Security Phase
Steps 3, 3.5, and 4).

Every route is protected by default; only the explicit auth pages
(`/auth/*`) and static assets (`/static/*`) are public. There is no
route-by-route opt-in list to keep in sync as new routes are added --
a new route is protected automatically simply by existing outside those
two prefixes.

Step 4 enforces the inactivity timeout (in addition to the absolute
expiry Step 3 already enforced) via
`app.core.auth.session.resolve_and_maintain_session()`: every request
that reaches this middleware and carries a still-valid session has that
session's inactivity window slid forward as a side effect of the same
lookup, and any request whose session has expired (either way) gets that
session's row deleted outright, not merely rejected -- see that
function's docstring for why a lazy delete-on-next-access is correct
here rather than needing a separate periodic sweep.

Every response this middleware handles -- protected or public, success or
redirect -- gets `Cache-Control: no-store, private` + `Pragma: no-cache`
so nothing sensitive (document bytes, page images, HTML with student
data, even the login form) is retained in any cache. `/static/*` assets
are the only exception, left cacheable since they carry no case data.

Step 3.5 adds CSRF validation for every mutating request (POST/PUT/
PATCH/DELETE) this middleware protects -- centrally, here, rather than
retrofitting a `csrf_token: str = Form(...)` parameter and `verify_csrf()`
call onto each of the ~24 existing non-auth state-changing routes. This
runs *after* the session check: an unauthenticated mutating request is
redirected to login (the more useful signal) rather than rejected for a
CSRF mismatch it was always going to fail regardless. `/auth/*` routes
are unaffected -- they're handled by the early-return public-path branch
above this check and keep validating CSRF themselves exactly as before
(Security Phase Step 2). A raised HTTPException would not work here the
way it does in a route handler: exceptions raised directly inside
`BaseHTTPMiddleware.dispatch()` (as opposed to ones that propagate up
through `call_next()`) bypass FastAPI's exception handlers entirely and
surface as an unhandled 500 -- see `app.core.auth.csrf.csrf_token_matches`.
So this returns a plain 403 `Response` directly, the same pattern already
used here for the login redirect.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.core.auth.csrf import csrf_token_matches, extract_submitted_csrf_token
from app.core.auth.session import SESSION_COOKIE_NAME, resolve_and_maintain_session

_PUBLIC_PATH_PREFIXES = ("/auth/",)
_STATIC_PATH_PREFIX = "/static/"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_public_auth_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _apply_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


class AuthEnforcementMiddleware(BaseHTTPMiddleware):
    """Redirects any request outside `/auth/*` and `/static/*` to
    `/auth/login` unless it carries a valid session, and rejects any
    mutating request among those with a missing or invalid CSRF token.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if path.startswith(_STATIC_PATH_PREFIX):
            return await call_next(request)

        if _is_public_auth_path(path):
            response = await call_next(request)
            _apply_no_store_headers(response)
            return response

        session_factory = request.app.state.session_factory
        with session_factory() as db:
            session = resolve_and_maintain_session(request, db)

        if session is None:
            response = RedirectResponse(url="/auth/login", status_code=303)
            # A session that just expired (rather than one that was never
            # there) still has its now-stale cookie on the request --
            # resolve_and_maintain_session() already deleted the row, but
            # the browser needs telling too, or it would keep presenting
            # a session_id that no longer names anything.
            response.delete_cookie(SESSION_COOKIE_NAME)
            _apply_no_store_headers(response)
            return response

        if request.method in _MUTATING_METHODS:
            submitted_token = await extract_submitted_csrf_token(request)
            if not csrf_token_matches(request, submitted_token):
                response = Response("Invalid or missing CSRF token.", status_code=403)
                _apply_no_store_headers(response)
                return response

        response = await call_next(request)
        _apply_no_store_headers(response)
        return response
