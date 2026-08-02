"""Deny-by-default authentication enforcement (Security Phase Step 3).

Every route is protected by default; only the explicit auth pages
(`/auth/*`) and static assets (`/static/*`) are public. There is no
route-by-route opt-in list to keep in sync as new routes are added --
a new route is protected automatically simply by existing outside those
two prefixes.

Inactivity-timeout enforcement is deliberately deferred to Step 4 (see
`app.core.auth.session.is_session_valid`'s docstring) -- this middleware
only enforces absolute session expiry for now, via
`check_inactivity=False`.

Every response this middleware handles -- protected or public, success or
redirect -- gets `Cache-Control: no-store, private` + `Pragma: no-cache`
so nothing sensitive (document bytes, page images, HTML with student
data, even the login form) is retained in any cache. `/static/*` assets
are the only exception, left cacheable since they carry no case data.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.core.auth.session import get_current_session

_PUBLIC_PATH_PREFIXES = ("/auth/",)
_STATIC_PATH_PREFIX = "/static/"


def _is_public_auth_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _apply_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


class AuthEnforcementMiddleware(BaseHTTPMiddleware):
    """Redirects any request outside `/auth/*` and `/static/*` to
    `/auth/login` unless it carries a valid session.
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
            session = get_current_session(request, db, check_inactivity=False)

        if session is None:
            response = RedirectResponse(url="/auth/login", status_code=303)
            _apply_no_store_headers(response)
            return response

        response = await call_next(request)
        _apply_no_store_headers(response)
        return response
