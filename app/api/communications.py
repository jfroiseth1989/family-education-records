"""Communications routes: Yahoo account connect/disconnect (Communications
Phase Step 2).

No email import, IMAP connectivity, thread/attachment handling, or
manual .eml upload yet -- see docs/COMMUNICATIONS_PLAN.md. This is
deliberately the entire surface Step 2 builds: a page to view connected
accounts, a form to connect a new Yahoo mailbox by app password, and a
disconnect action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.communications.accounts import connect_yahoo_account, disconnect_account
from app.core.communications.credentials import KeyringUnavailableError
from app.db.models import CommunicationAccount

router = APIRouter(prefix="/communications", tags=["communications"])


def _list_accounts(db: Session) -> list[CommunicationAccount]:
    return list(
        db.scalars(
            select(CommunicationAccount).order_by(CommunicationAccount.connected_at.desc())
        ).all()
    )


@router.get("", response_class=HTMLResponse)
def get_communications_home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communications_home.html",
        {"accounts": _list_accounts(db), "connect_error": None},
    )


@router.post("/yahoo/connect")
def post_connect_yahoo(
    request: Request,
    email_address: str = Form(""),
    app_password: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
):
    """Save a Yahoo app-password credential and record the account.

    Never echoes `app_password` back in any response -- on a validation
    or keyring failure, the form re-renders with the email address the
    user typed but the password field is always left blank, exactly like
    a normal browser password field on a failed submit.
    """
    email_address = email_address.strip()
    if not email_address:
        raise HTTPException(status_code=400, detail="A Yahoo email address is required.")
    if not app_password.strip():
        raise HTTPException(status_code=400, detail="A Yahoo app password is required.")

    try:
        connect_yahoo_account(
            db, email_address=email_address, app_password=app_password, actor=actor
        )
    except KeyringUnavailableError as exc:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "communications_home.html",
            {"accounts": _list_accounts(db), "connect_error": str(exc)},
            status_code=503,
        )

    return RedirectResponse(url="/communications", status_code=303)


@router.post("/{account_id}/disconnect")
def post_disconnect_account(
    account_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Delete the account's stored credential and mark it disconnected.

    Never deletes or modifies any `Communication`/`CommunicationAttachment`/
    `Document` row imported through this account -- disconnecting only
    ever removes the credential and the ability to sync further, never
    already-imported data.
    """
    account = db.get(CommunicationAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

    disconnect_account(db, account)
    return RedirectResponse(url="/communications", status_code=303)
