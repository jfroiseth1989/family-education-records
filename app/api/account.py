"""Authenticated account-management routes (Security Phase Step 5).

Deliberately *not* under `/auth/*`: that prefix is
`app.core.auth.enforcement.AuthEnforcementMiddleware`'s public allowlist,
and every route here needs a valid session -- an unauthenticated caller
must not be able to invalidate the real owner's live recovery key.
Living outside `/auth/*` and `/static/*` is all that's required for the
deny-by-default middleware to protect these automatically, the same way
it already protects every other existing route; nothing here needs any
route-specific enforcement logic of its own. CSRF is likewise already
covered application-wide by that same middleware for the POST below.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.csrf import get_or_create_csrf_token, set_csrf_cookie
from app.core.auth.recovery import issue_recovery_key
from app.db.models import AppAuth

router = APIRouter(prefix="/account", tags=["account"])


def _get_auth(db: Session) -> AppAuth:
    # A protected route is only ever reachable with a valid session,
    # which only ever exists because an AppAuth row was created first
    # (app/api/auth.py) -- so unlike that module's `_get_auth()`, this
    # one doesn't need to handle "no account yet" as a real case.
    return db.scalars(select(AppAuth)).one()


@router.get("/recovery-key", response_class=HTMLResponse)
def get_recovery_key_settings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Show when the recovery key was last (re)generated and a button to
    replace it. Never displays the key itself here -- only the one-time
    display right after generation ever shows the raw value.
    """
    auth = _get_auth(db)
    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "account_recovery_key.html",
        {"recovery_key_updated_at": auth.recovery_key_updated_at, "csrf_token": csrf_token},
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/recovery-key/regenerate")
def post_regenerate_recovery_key(request: Request, db: Session = Depends(get_db)) -> Response:
    """Issue a brand-new recovery key, immediately invalidating the old
    one (`issue_recovery_key()` overwrites the stored hash) -- does not
    touch the password, sessions, or anything else; only the recovery
    key changes.
    """
    auth = _get_auth(db)
    new_recovery_key = issue_recovery_key(auth)
    db.commit()

    csrf_token = get_or_create_csrf_token(request)
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "auth_recovery_key.html",
        {"recovery_key": new_recovery_key, "error": None, "csrf_token": csrf_token},
    )
    set_csrf_cookie(response, csrf_token)
    return response
