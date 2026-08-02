"""Server-side session issuance/lookup (Security Phase Step 2).

Sessions are opaque tokens stored in `app_sessions` (see the AppSession
model docstring) -- the cookie only ever carries the token, never any
session data itself. This module enforces nothing on its own: reading an
invalid/missing/expired session here just returns None. Route-level
enforcement (blocking access without a valid session) is a separate,
later step; in this step, `get_current_session()` is used only to decide
what the header shows (Log in vs. Lock/Log out) and by the login/setup
routes to issue a session on success.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppAuth, AppSession

SESSION_COOKIE_NAME = "ferchronos_session"
_TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(db: Session, auth: AppAuth) -> AppSession:
    """Issue a brand-new session for `auth`'s owner and stage it for
    insert.

    Always mints a fresh, cryptographically random `session_id` --
    never reuses or "upgrades" any pre-existing cookie a request might
    have presented, which is what rules out session fixation by
    construction (see the AppSession model docstring). Does not commit;
    the caller controls the transaction boundary, matching every other
    core service function in this codebase.
    """
    now = _now()
    session = AppSession(
        session_id=secrets.token_urlsafe(_TOKEN_BYTES),
        last_activity_at=now,
        expires_at=now + timedelta(hours=auth.session_absolute_expiry_hours),
    )
    db.add(session)
    return session


def touch_session(session: AppSession) -> None:
    """Slide the inactivity window forward. Not called by any route yet
    in this step (there is no enforcement middleware to call it from);
    provided now, with its own test, so the later enforcement step
    reuses this exact function rather than re-deriving the logic.
    """
    session.last_activity_at = _now()


def is_session_valid(session: AppSession, auth: AppAuth) -> bool:
    """True iff `session` hasn't hit its absolute expiry or its
    inactivity timeout, given `auth`'s currently configured limits.

    SQLite doesn't reliably round-trip tzinfo on `DateTime(timezone=True)`
    columns -- `session.expires_at`/`last_activity_at` come back naive
    after a genuine cross-request round-trip (a fresh session per
    request, as every real HTTP request uses), while `_now()` here is
    always timezone-aware. Comparing them directly raises TypeError.
    Every such column in this app is effectively naive-UTC on disk
    regardless of what wrote it, so stripping tzinfo from `now` before
    comparing is the correct normalization, not a workaround -- see the
    identical, earlier-discovered case in
    app/core/timeline/service.py::_validate_precision().
    """
    now = _now().replace(tzinfo=None)
    if session.expires_at.replace(tzinfo=None) <= now:
        return False
    inactivity_cutoff = session.last_activity_at.replace(tzinfo=None) + timedelta(
        minutes=auth.inactivity_lock_minutes
    )
    return inactivity_cutoff > now


def get_current_session(request: Request, db: Session) -> AppSession | None:
    """Look up the session named by `request`'s cookie, or None if there
    isn't one, it doesn't exist, or it's no longer valid.

    Never deletes an invalid row itself -- that's an explicit action
    (logout/manual lock) or a future expiry sweep, never a side effect
    of merely reading. Purely informational in this step; nothing yet
    uses this to block access (see module docstring).
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None

    session = db.get(AppSession, session_id)
    if session is None:
        return None

    auth = db.scalars(select(AppAuth)).first()
    if auth is None or not is_session_valid(session, auth):
        return None

    return session


def delete_session(db: Session, session_id: str) -> None:
    """Hard-delete the session row for `session_id`, if any -- logout
    and manual lock both call this. A session row is ephemeral security
    state, not an educational record (see the AppSession model
    docstring), so hard delete is correct here, unlike everywhere else
    in this schema. Does not commit; the caller controls the transaction
    boundary.
    """
    session = db.get(AppSession, session_id)
    if session is not None:
        db.delete(session)
