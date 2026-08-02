"""Server-side session issuance/lookup (Security Phase Steps 2-4).

Sessions are opaque tokens stored in `app_sessions` (see the AppSession
model docstring) -- the cookie only ever carries the token, never any
session data itself. `get_current_session()` is a pure read: an
invalid/missing/expired session just returns None, no side effects. It's
used by base.html's header display and by the login/setup routes to
issue a session on success.

`resolve_and_maintain_session()` is the other, request-mutating kind of
lookup, reserved for `app.core.auth.enforcement.AuthEnforcementMiddleware`
(Security Phase Step 4): a valid session's inactivity window is slid
forward, and an invalid one's row is hard-deleted outright rather than
merely rejected in place -- see that function's docstring.
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
    """Slide the inactivity window forward by setting `last_activity_at`
    to now. Called by `resolve_and_maintain_session()` -- and only there
    -- exactly once per request that turns out to carry a valid session,
    which is what "update session activity only after a valid
    authenticated request" (Security Phase Step 4) means in practice:
    a request that never had a session, or whose session already failed
    validation, never reaches this call.
    """
    session.last_activity_at = _now()


def is_session_valid(session: AppSession, auth: AppAuth, *, check_inactivity: bool = True) -> bool:
    """True iff `session` hasn't hit its absolute expiry and (when
    `check_inactivity` is True) hasn't hit its inactivity timeout either,
    given `auth`'s currently configured limits.

    `check_inactivity=False` was how the Step 3 enforcement middleware
    called this: absolute expiry was enforced starting in Step 3, but
    inactivity-timeout enforcement was deliberately deferred to Step 4,
    since enforcing it without also wiring `touch_session()` into the
    request path would let a session go stale far past the intended
    15-minute window without ever sliding forward. Step 4 now does both
    together -- see `resolve_and_maintain_session()`, which calls this
    with the default `check_inactivity=True`. The parameter still
    defaults to `True` and stays available for any caller (present or
    future) that only cares about absolute expiry.

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
    if not check_inactivity:
        return True
    inactivity_cutoff = session.last_activity_at.replace(tzinfo=None) + timedelta(
        minutes=auth.inactivity_lock_minutes
    )
    return inactivity_cutoff > now


def get_current_session(
    request: Request, db: Session, *, check_inactivity: bool = True
) -> AppSession | None:
    """Look up the session named by `request`'s cookie, or None if there
    isn't one, it doesn't exist, or it's no longer valid.

    Never deletes an invalid row itself -- that's an explicit action
    (logout/manual lock) or a future expiry sweep, never a side effect
    of merely reading. `check_inactivity` is passed straight through to
    `is_session_valid` -- see its docstring for why the Step 3 enforcement
    middleware calls this with `check_inactivity=False`.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None

    session = db.get(AppSession, session_id)
    if session is None:
        return None

    auth = db.scalars(select(AppAuth)).first()
    if auth is None or not is_session_valid(session, auth, check_inactivity=check_inactivity):
        return None

    return session


def resolve_and_maintain_session(request: Request, db: Session) -> AppSession | None:
    """The authoritative, request-mutating session check for
    `AuthEnforcementMiddleware` (Security Phase Step 4) -- and only for
    it; every other caller wants the side-effect-free
    `get_current_session()` instead.

    A valid session has its inactivity window slid forward
    (`touch_session()`) and the update committed, so "session activity"
    reflects real authenticated traffic, not merely a still-open cookie.
    An invalid session (hit either its absolute expiry or its inactivity
    cutoff) has its row hard-deleted and committed before returning None
    -- "expired or inactive sessions must be deleted and require a full
    password login" is a hard requirement, not just "stop honoring the
    cookie": leaving the row in place would mean a session that merely
    looks expired today could, if the clock were ever wrong or the row
    otherwise reused, still exist to be resurrected, and it would also
    leave stale rows accumulating forever with nothing to prune them.
    A missing cookie or a cookie naming a row that's already gone is not
    an error -- both simply return None with nothing to delete.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None

    session = db.get(AppSession, session_id)
    if session is None:
        return None

    auth = db.scalars(select(AppAuth)).first()
    if auth is None or not is_session_valid(session, auth, check_inactivity=True):
        delete_session(db, session_id)
        db.commit()
        return None

    touch_session(session)
    db.commit()
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


def delete_all_sessions(db: Session) -> None:
    """Hard-delete every session row -- called after a password reset via
    recovery key (Security Phase Step 5): "invalidate existing sessions
    after a password reset" means every session anywhere this app might
    be open, not just the one (if any) the reset request happened to
    carry, since a recovery reset is precisely the scenario where the
    owner may not have had a valid session to begin with. Does not
    commit; the caller controls the transaction boundary.
    """
    for session in db.scalars(select(AppSession)).all():
        db.delete(session)
