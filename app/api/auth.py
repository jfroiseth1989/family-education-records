"""First-run setup, login, lock, and logout routes (Security Phase Steps
2 and 4).

Now enforced application-wide by
`app.core.auth.enforcement.AuthEnforcementMiddleware` -- every route
outside this router's `/auth/*` prefix requires a valid session. These
routes stay the one deliberately public path a locked-out or logged-out
owner can always reach. See app/core/auth/session.py and
app/core/auth/csrf.py, and docs/PRIVACY_SECURITY.md §6.

Every state-changing route here validates a CSRF token before doing
anything else. Login failures always show the same generic message
("Incorrect password.") regardless of the underlying reason, and
password verification always goes through
app.core.auth.passwords.verify_password_constant_time() -- see that
function's docstring for why: neither the wording nor the timing of a
failed login should ever reveal information about this application's
account state.

Step 4 adds local failed-login throttling, backed by
`AppAuth.failed_login_attempts`/`locked_until` (present in the schema
since Step 1, unused until now -- see that model's docstring). The
5th consecutive failure locks login out for 60 seconds; each further
failure while still counting doubles that, capped at 15 minutes
(`_lockout_seconds_for()`). A successful login resets the counter and
clears the lock -- see `post_login()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.csrf import get_or_create_csrf_token, set_csrf_cookie, verify_csrf
from app.core.auth.passwords import hash_password, verify_password_constant_time
from app.core.auth.session import SESSION_COOKIE_NAME, create_session, delete_session
from app.db.models import AppAuth

router = APIRouter(prefix="/auth", tags=["auth"])

_MIN_PASSWORD_LENGTH = 8

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_INITIAL_SECONDS = 60
_LOCKOUT_MAX_SECONDS = 15 * 60
_LOCKOUT_MESSAGE = "Too many failed attempts. Please wait a few minutes and try again."


def _get_auth(db: Session) -> AppAuth | None:
    return db.scalars(select(AppAuth)).first()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lockout_seconds_for(failed_attempts: int) -> int:
    """Escalating lockout duration for the `failed_attempts`-th
    consecutive failure: 60s at the 5th, doubling each failure after
    that, capped at 900s (15 minutes) -- the approved defaults.
    """
    doublings = failed_attempts - _LOCKOUT_THRESHOLD
    return min(_LOCKOUT_INITIAL_SECONDS * (2**doublings), _LOCKOUT_MAX_SECONDS)


def _is_locked_out(auth: AppAuth) -> bool:
    if auth.locked_until is None:
        return False
    # SQLite doesn't reliably round-trip tzinfo -- see the identical,
    # earlier-discovered case in app/core/auth/session.py::is_session_valid.
    return auth.locked_until.replace(tzinfo=None) > _now().replace(tzinfo=None)


def _set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="strict",
        # Loopback-only plain HTTP by design, no TLS layer -- see
        # docs/PRIVACY_SECURITY.md §2. `Secure` would add nothing here.
        secure=False,
    )


@router.get("/setup", response_class=HTMLResponse)
def get_setup(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Show the first-run "create a password" form.

    Redirects to /auth/login if setup has already happened -- setup is a
    one-time action for this application's single owner; visiting this
    URL again must never offer to recreate or overwrite credentials.
    """
    if _get_auth(db) is not None:
        return RedirectResponse(url="/auth/login", status_code=303)

    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request, "auth_setup.html", {"error": None, "csrf_token": csrf_token}
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/setup")
def post_setup(
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Create the single owner account. Refuses to run a second time --
    if an AppAuth row already exists (e.g. a second browser tab raced
    this one), redirects to /auth/login instead of overwriting it.
    """
    verify_csrf(request, csrf_token)

    if _get_auth(db) is not None:
        return RedirectResponse(url="/auth/login", status_code=303)

    error = None
    if len(password) < _MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
    elif password != confirm_password:
        error = "Passwords do not match."

    if error:
        new_csrf_token = get_or_create_csrf_token(request)
        templates = request.app.state.templates
        response = templates.TemplateResponse(
            request,
            "auth_setup.html",
            {"error": error, "csrf_token": new_csrf_token},
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf_token)
        return response

    # Recovery-key generation is Security Phase Step 5, not this step --
    # recovery_key_hash stays NULL here on purpose; see the AppAuth
    # model docstring.
    auth = AppAuth(password_hash=hash_password(password))
    db.add(auth)
    db.flush()  # assigns nothing session-relevant, but keeps the pattern consistent with the rest of this codebase

    session = create_session(db, auth)
    db.commit()

    response = RedirectResponse(url="/cases", status_code=303)
    _set_session_cookie(response, session.session_id)
    return response


@router.get("/login", response_class=HTMLResponse)
def get_login(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Show the login form, or redirect to /auth/setup if no account
    exists yet -- there is nothing to log into before first-run setup.
    """
    if _get_auth(db) is None:
        return RedirectResponse(url="/auth/setup", status_code=303)

    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request, "auth_login.html", {"error": None, "csrf_token": csrf_token}
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _login_error_response(request: Request, message: str, status_code: int) -> Response:
    new_csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "auth_login.html",
        {"error": message, "csrf_token": new_csrf_token},
        status_code=status_code,
    )
    set_csrf_cookie(response, new_csrf_token)
    return response


@router.post("/login")
def post_login(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, csrf_token)

    auth = _get_auth(db)

    # A currently-locked account is rejected before spending an Argon2
    # verify on it, deliberately: extending the lock on every hammered
    # request during an active lockout would let a sustained attacker
    # keep the owner locked out indefinitely, which the escalating-but-
    # capped design is meant to prevent. This doesn't reopen the timing
    # side-channel verify_password_constant_time() closes -- that
    # channel is "does this account exist at all", which is already
    # settled by the time login is reachable (see get_login: no account
    # redirects to /auth/setup instead of ever rendering this form).
    if auth is not None and _is_locked_out(auth):
        return _login_error_response(request, _LOCKOUT_MESSAGE, 429)

    # verify_password_constant_time() takes the same code path (a real
    # Argon2 verify, against a dummy hash if auth is None) either way --
    # see that function's docstring.
    password_hash = auth.password_hash if auth is not None else None
    if not verify_password_constant_time(password, password_hash):
        if auth is not None:
            auth.failed_login_attempts += 1
            if auth.failed_login_attempts >= _LOCKOUT_THRESHOLD:
                auth.locked_until = _now() + timedelta(
                    seconds=_lockout_seconds_for(auth.failed_login_attempts)
                )
            db.commit()
            if _is_locked_out(auth):
                return _login_error_response(request, _LOCKOUT_MESSAGE, 429)
        return _login_error_response(request, "Incorrect password.", 401)

    auth.failed_login_attempts = 0
    auth.locked_until = None

    session = create_session(db, auth)
    db.commit()

    response = RedirectResponse(url="/cases", status_code=303)
    _set_session_cookie(response, session.session_id)
    return response


@router.post("/lock")
def post_lock(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """End the current session and send the owner back to the login
    screen with "locked" framing.

    Functionally identical to logout (both fully end the session and
    require the full password again -- there is no weaker "quick
    unlock"); the only difference is the `locked=1` query flag, which
    changes only the login page's copy ("FERChronos is locked" instead
    of a plain "Log in").
    """
    verify_csrf(request, csrf_token)

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(db, session_id)
        db.commit()

    response = RedirectResponse(url="/auth/login?locked=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.post("/logout")
def post_logout(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(db, session_id)
        db.commit()

    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
