"""First-run setup, login, lock, logout, and password-recovery routes
(Security Phase Steps 2, 4, and 5).

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
clears the lock -- see `post_login()`. Step 5's recovery flow
(`post_recover()`) shares this exact same counter/lock: a wrong recovery
key is, threat-model-wise, the same kind of "someone without this
account's secret" event as a wrong password, and reusing it needs no
schema change.

Step 5 adds recovery-key issuance and password recovery. A key is
generated and shown exactly once, at the end of `post_setup()` and again
at the end of `post_recover()` (which rotates it -- a recovery key is
single-use, since leaving it valid forever after using it once would
turn it into a permanent secondary password with no rotation). The
authenticated owner can also rotate it deliberately, at any time, via
`app.api.account` -- deliberately *not* under this router's `/auth/*`
prefix, because that prefix is the enforcement middleware's public
allowlist and rotating a live recovery key must require a valid session,
unlike everything else here.
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
from app.core.auth.recovery import issue_recovery_key, normalize_recovery_key
from app.core.auth.session import (
    SESSION_COOKIE_NAME,
    create_session,
    delete_all_sessions,
    delete_session,
)
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


def _record_failed_attempt(db: Session, auth: AppAuth) -> bool:
    """Shared by `post_login()` and `post_recover()`: one more failure
    (wrong password or wrong recovery key -- either way, someone without
    this account's secret) against the same counter/lock. Commits and
    returns whether the account is now locked out as a result.
    """
    auth.failed_login_attempts += 1
    if auth.failed_login_attempts >= _LOCKOUT_THRESHOLD:
        auth.locked_until = _now() + timedelta(seconds=_lockout_seconds_for(auth.failed_login_attempts))
    db.commit()
    return _is_locked_out(auth)


def _reset_lockout(auth: AppAuth) -> None:
    auth.failed_login_attempts = 0
    auth.locked_until = None


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="strict",
        # Loopback-only plain HTTP by design, no TLS layer -- see
        # docs/PRIVACY_SECURITY.md §2. `Secure` would add nothing here.
        secure=False,
    )


def _recovery_key_response(request: Request, recovery_key: str, *, error: str | None = None) -> Response:
    """Render the shared "here is your recovery key -- confirm you saved
    it" page. Every caller -- setup, recovery, an authenticated rotation
    in app.api.account, or an unconfirmed resubmission from
    `post_acknowledge_recovery_key()` -- passes the real key value each
    time; it round-trips through that route's hidden form field on a
    retry rather than this ever persisting the raw key server-side.
    """
    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "auth_recovery_key.html",
        {"recovery_key": recovery_key, "error": error, "csrf_token": csrf_token},
    )
    set_csrf_cookie(response, csrf_token)
    return response


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

    auth = AppAuth(password_hash=hash_password(password))
    db.add(auth)
    db.flush()  # assigns nothing session-relevant, but keeps the pattern consistent with the rest of this codebase

    recovery_key = issue_recovery_key(auth)
    session = create_session(db, auth)
    db.commit()

    # Straight to the recovery-key display, not /cases -- the owner is
    # already fully authenticated (the session above is real and its
    # cookie is set on this very response), but must see and acknowledge
    # the key before going anywhere else in the normal flow.
    response = _recovery_key_response(request, recovery_key)
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
        if auth is not None and _record_failed_attempt(db, auth):
            return _login_error_response(request, _LOCKOUT_MESSAGE, 429)
        return _login_error_response(request, "Incorrect password.", 401)

    _reset_lockout(auth)

    session = create_session(db, auth)
    db.commit()

    response = RedirectResponse(url="/cases", status_code=303)
    _set_session_cookie(response, session.session_id)
    return response


@router.get("/recover", response_class=HTMLResponse)
def get_recover(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Show the "reset your password with your recovery key" form, or
    redirect to /auth/setup if no account exists yet -- same reasoning
    as get_login().
    """
    if _get_auth(db) is None:
        return RedirectResponse(url="/auth/setup", status_code=303)

    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request, "auth_recover.html", {"error": None, "csrf_token": csrf_token}
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _recover_error_response(request: Request, message: str, status_code: int) -> Response:
    new_csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "auth_recover.html",
        {"error": message, "csrf_token": new_csrf_token},
        status_code=status_code,
    )
    set_csrf_cookie(response, new_csrf_token)
    return response


@router.post("/recover")
def post_recover(
    request: Request,
    recovery_key: str = Form(...),
    new_password: str = Form(...),
    confirm_new_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Reset the password using the recovery key, entirely in place of
    the forgotten one -- there is no security question, hint, hidden
    master password, or cloud/support-desk path around this (see this
    module's and app/core/auth/recovery.py's docstrings). On success:
    the password is replaced, the recovery key is rotated (the one just
    used is never valid again -- see `issue_recovery_key()`), every
    existing session anywhere is invalidated, and the owner is signed
    into a brand-new session and shown the new key to save.
    """
    verify_csrf(request, csrf_token)

    auth = _get_auth(db)

    # Same shared lockout as post_login() -- see _record_failed_attempt()
    # and this module's docstring for why reusing it here is deliberate.
    if auth is not None and _is_locked_out(auth):
        return _recover_error_response(request, _LOCKOUT_MESSAGE, 429)

    # verify_password_constant_time() works for any Argon2id-hashed
    # secret, not just a password -- recovery_key_hash is exactly such a
    # hash (or None, for an account that predates Step 5 or has never had
    # a key issued, which this call handles identically to "no account
    # yet": a real Argon2 verify against a dummy hash, same rejection).
    recovery_key_hash = auth.recovery_key_hash if auth is not None else None
    if not verify_password_constant_time(normalize_recovery_key(recovery_key), recovery_key_hash):
        if auth is not None and _record_failed_attempt(db, auth):
            return _recover_error_response(request, _LOCKOUT_MESSAGE, 429)
        return _recover_error_response(request, "Invalid recovery key.", 401)

    error = None
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
    elif new_password != confirm_new_password:
        error = "Passwords do not match."
    if error:
        return _recover_error_response(request, error, 400)

    auth.password_hash = hash_password(new_password)
    auth.password_updated_at = _now()
    _reset_lockout(auth)
    delete_all_sessions(db)

    new_recovery_key = issue_recovery_key(auth)
    session = create_session(db, auth)
    db.commit()

    response = _recovery_key_response(request, new_recovery_key)
    _set_session_cookie(response, session.session_id)
    return response


@router.post("/recovery-key/acknowledge")
def post_acknowledge_recovery_key(
    request: Request,
    recovery_key: str = Form(...),
    confirmed: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """The "I have saved this recovery key" step at the end of setup,
    recovery, or an authenticated rotation (app.api.account). Lives
    under the public `/auth/*` prefix on purpose -- unlike those routes,
    it doesn't itself require a session (the enforcement middleware
    never checks one here), but that's safe rather than a gap: this
    route performs no mutation at all. The recovery-key hash was already
    committed by whichever route sent the owner here; this one only
    echoes the same key back on an unconfirmed submission (never a fresh
    one) and redirects to /cases on confirmation -- a redirect to a
    protected route the enforcement middleware still gates normally, so
    an unauthenticated caller gains nothing by hitting this directly.

    Unconfirmed submissions (the checkbox wasn't checked) re-show the
    exact same key rather than losing it -- it round-trips through a
    hidden field precisely so a missed checkbox never means "generate an
    unrelated new key just to see it again."
    """
    verify_csrf(request, csrf_token)

    if not confirmed:
        return _recovery_key_response(
            request, recovery_key, error="Please confirm you have saved your recovery key."
        )

    return RedirectResponse(url="/cases", status_code=303)


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
