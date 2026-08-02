"""First-run setup, login, lock, and logout routes (Security Phase Step 2).

Deliberately **not enforced** yet -- no other route in this application
checks for a session; that is a separate, later step (a deny-by-default
middleware). These routes exist and are fully tested in isolation first,
so that later step builds on a working foundation instead of landing
everything at once. See app/core/auth/session.py and
app/core/auth/csrf.py, and docs/PRIVACY_SECURITY.md §6.

Every state-changing route here validates a CSRF token before doing
anything else. Login failures always show the same generic message
("Incorrect password.") regardless of the underlying reason, and
password verification always goes through
app.core.auth.passwords.verify_password_constant_time() -- see that
function's docstring for why: neither the wording nor the timing of a
failed login should ever reveal information about this application's
account state.
"""

from __future__ import annotations

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


def _get_auth(db: Session) -> AppAuth | None:
    return db.scalars(select(AppAuth)).first()


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


@router.post("/login")
def post_login(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, csrf_token)

    auth = _get_auth(db)
    # verify_password_constant_time() takes the same code path (a real
    # Argon2 verify, against a dummy hash if auth is None) either way --
    # see that function's docstring. The error message below is
    # identical regardless of which branch produced the failure.
    password_hash = auth.password_hash if auth is not None else None
    if not verify_password_constant_time(password, password_hash):
        new_csrf_token = get_or_create_csrf_token(request)
        templates = request.app.state.templates
        response = templates.TemplateResponse(
            request,
            "auth_login.html",
            {"error": "Incorrect password.", "csrf_token": new_csrf_token},
            status_code=401,
        )
        set_csrf_cookie(response, new_csrf_token)
        return response

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
