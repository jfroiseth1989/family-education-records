"""Communications routes (Communications Phase Steps 2-3): Yahoo account
connect/disconnect, and manual .eml upload/viewing.

No IMAP connectivity, thread reconstruction, attachment-to-Document
promotion, or search yet -- see docs/COMMUNICATIONS_PLAN.md. Manual
upload here is deliberately independent of any connected mailbox: it
never reads `CommunicationAccount`, never checks connection status, and
works identically whether zero or several Yahoo accounts are connected.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db, get_vault
from app.core.communications.accounts import connect_yahoo_account, disconnect_account
from app.core.communications.credentials import KeyringUnavailableError
from app.core.communications.ingestion import DuplicateCommunicationError, import_eml_file
from app.core.vault import VaultLayout
from app.db.models import Case, Communication, CommunicationAccount

router = APIRouter(prefix="/communications", tags=["communications"])


def _list_accounts(db: Session) -> list[CommunicationAccount]:
    return list(
        db.scalars(
            select(CommunicationAccount).order_by(CommunicationAccount.connected_at.desc())
        ).all()
    )


def _list_recent_communications(db: Session, limit: int = 100) -> list[Communication]:
    return list(
        db.scalars(
            select(Communication)
            .where(Communication.deleted_at.is_(None))
            .order_by(Communication.imported_at.desc())
            .limit(limit)
        ).all()
    )


def _list_cases(db: Session) -> list[Case]:
    return list(db.scalars(select(Case).order_by(Case.label)).all())


def _save_upload_to_temp(upload: UploadFile) -> Path:
    """Spool an uploaded file to a temp path so import can hash/copy it.

    Same convention as app/api/documents.py::_save_upload_to_temp -- the
    temp file is a working copy of what the browser sent, not the
    "original" in the chain-of-custody sense; the copy import makes into
    the vault from this temp file is. Callers delete the temp file once
    import has copied it.
    """
    suffix = Path(upload.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(upload.file, tmp)
        return Path(tmp.name)


def _home_context(db: Session, *, connect_error: str | None = None, upload_error: str | None = None) -> dict:
    return {
        "accounts": _list_accounts(db),
        "connect_error": connect_error,
        "communications": _list_recent_communications(db),
        "cases": _list_cases(db),
        "upload_error": upload_error,
    }


@router.get("", response_class=HTMLResponse)
def get_communications_home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "communications_home.html", _home_context(db))


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
            _home_context(db, connect_error=str(exc)),
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


@router.post("/upload")
def post_upload_email(
    request: Request,
    file: UploadFile,
    case_id: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
):
    """Manually import one `.eml` file for a chosen student -- completely
    independent of whether any Yahoo account is connected (this route
    never reads `CommunicationAccount` at all). Preserves the raw
    message and its attachments read-only, hashed, with a custody event,
    exactly like document ingestion -- see
    app/core/communications/ingestion.py.
    """
    if not case_id.strip() or not case_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="A student must be selected.")
    case = db.get(Case, int(case_id))
    if case is None:
        raise HTTPException(status_code=400, detail="Selected student was not found.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="An .eml file is required.")
    if Path(file.filename).suffix.lower() != ".eml":
        raise HTTPException(status_code=400, detail="Only .eml files are supported for manual import.")

    temp_path = _save_upload_to_temp(file)
    try:
        try:
            communication = import_eml_file(
                db,
                vault,
                case,
                source_file_path=temp_path,
                original_filename=file.filename,
                actor=actor,
            )
        except DuplicateCommunicationError as exc:
            db.rollback()
            templates = request.app.state.templates
            return templates.TemplateResponse(
                request,
                "communications_home.html",
                _home_context(db, upload_error=str(exc)),
                status_code=409,
            )

        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return RedirectResponse(url=f"/communications/email/{communication.communication_id}", status_code=303)


@router.get("/email/{communication_id}", response_class=HTMLResponse)
def get_communication_detail(
    communication_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    communication = db.get(Communication, communication_id)
    if communication is None:
        raise HTTPException(status_code=404, detail=f"Communication {communication_id} not found.")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_detail.html",
        {"communication": communication, "attachments": communication.attachments},
    )
