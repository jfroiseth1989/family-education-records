"""Communications routes (Communications Phase Steps 2-6): Yahoo account
connect/disconnect, manual .eml upload/viewing, email thread views,
attachment review/promotion to Documents, and Communications search.

No IMAP connectivity or bulk attachment review yet -- see
docs/COMMUNICATIONS_PLAN.md. Manual upload here is deliberately
independent of any connected mailbox: it never reads
`CommunicationAccount`, never checks connection status, and works
identically whether zero or several Yahoo accounts are connected.
Thread reconstruction (`app/core/communications/thread_rebuild.py`) runs
automatically inside `import_eml_file()`; these routes only ever read the
resulting `communication_threads` rows, never write to them.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db, get_vault
from app.core.communications.accounts import connect_yahoo_account, disconnect_account
from app.core.communications.attachment_metadata import (
    compose_notes,
    suggest_date_received,
    suggest_source,
)
from app.core.communications.credentials import KeyringUnavailableError
from app.core.communications.ingestion import DuplicateCommunicationError, import_eml_file
from app.core.communications.promotion import (
    AttachmentNotPromotableError,
    exclude_attachment,
    leave_attachment_with_email,
    promote_attachment_to_document,
)
from app.core.indexing.communications_search import search_communications
from app.core.vault import VaultLayout
from app.db.models import (
    Case,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationDocumentLink,
    CommunicationThread,
    DocumentType,
)

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

    document_links = list(
        db.scalars(
            select(CommunicationDocumentLink)
            .where(CommunicationDocumentLink.communication_id == communication_id)
            .order_by(CommunicationDocumentLink.linked_at)
        ).all()
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_detail.html",
        {
            "communication": communication,
            "attachments": communication.attachments,
            "document_links": document_links,
        },
    )


def _list_threads(db: Session, limit: int = 100) -> list[CommunicationThread]:
    return list(
        db.scalars(
            select(CommunicationThread)
            .order_by(CommunicationThread.last_message_at.desc())
            .limit(limit)
        ).all()
    )


def _sorted_thread_messages(thread: CommunicationThread) -> list[Communication]:
    return sorted(
        thread.communications,
        key=lambda c: (c.sent_at or c.received_at or c.imported_at, c.communication_id),
    )


@router.get("/threads", response_class=HTMLResponse)
def get_communication_threads(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "communication_threads.html", {"threads": _list_threads(db)}
    )


@router.get("/threads/{thread_id}", response_class=HTMLResponse)
def get_communication_thread_detail(
    thread_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    thread = db.get(CommunicationThread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found.")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_thread_detail.html",
        {"thread": thread, "messages": _sorted_thread_messages(thread)},
    )


# --- attachment review / promotion to Documents (Communications Phase Step 5) --


def _get_attachment_or_404(db: Session, attachment_id: int) -> CommunicationAttachment:
    attachment = db.get(CommunicationAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail=f"Attachment {attachment_id} not found.")
    return attachment


def _parse_optional_date(raw: str, field_label: str) -> date | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {field_label} '{value}' (expected YYYY-MM-DD)."
        ) from exc


def _collect_field_provenance(**sources: str) -> dict[str, str]:
    return {field: value for field, value in sources.items() if value.strip()}


def _review_context(
    db: Session,
    attachment: CommunicationAttachment,
    *,
    promotion_message: str | None = None,
    promoted_document=None,
) -> dict:
    communication = attachment.communication
    document_types = list(
        db.scalars(select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)).all()
    )
    suggested_date_received = suggest_date_received(communication)
    return {
        "attachment": attachment,
        "communication": communication,
        "document_types": document_types,
        "suggested_type_id": attachment.suggested_document_type_id,
        "suggested_source": suggest_source(communication),
        "suggested_date_received": suggested_date_received.isoformat() if suggested_date_received else "",
        "suggested_notes": compose_notes(communication),
        "promotion_message": promotion_message,
        "promoted_document": promoted_document,
    }


@router.get("/attachments/{attachment_id}/file")
def download_attachment_file(
    attachment_id: int, db: Session = Depends(get_db), vault: VaultLayout = Depends(get_vault)
):
    """Serve the stored attachment file, read-only, for viewing/downloading."""
    attachment = _get_attachment_or_404(db, attachment_id)
    full_path = vault.root / attachment.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Stored attachment file is missing from the vault.")
    return FileResponse(
        path=full_path,
        filename=attachment.filename,
        media_type=attachment.mime_type or "application/octet-stream",
    )


@router.get("/attachments/{attachment_id}/review", response_class=HTMLResponse)
def get_attachment_review(
    attachment_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    attachment = _get_attachment_or_404(db, attachment_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "communication_attachment_review.html", _review_context(db, attachment)
    )


@router.post("/attachments/{attachment_id}/add-to-documents", response_class=HTMLResponse)
def post_add_attachment_to_documents(
    attachment_id: int,
    request: Request,
    document_type_id: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
    date_received: str = Form(""),
    document_type_source: str = Form(""),
    source_source: str = Form(""),
    date_received_source: str = Form(""),
    notes_source: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    """Add one attachment to Documents, reusing an existing Document by
    content hash rather than creating a duplicate -- see
    app/core/communications/promotion.py.

    Always re-renders the review page (rather than redirecting) so a
    duplicate outcome can surface "This document is already in
    FERChronos" in the same response, per docs/COMMUNICATIONS_PLAN.md
    Step 5.
    """
    attachment = _get_attachment_or_404(db, attachment_id)

    parsed_type_id = int(document_type_id) if document_type_id.strip() else None
    if parsed_type_id is not None and db.get(DocumentType, parsed_type_id) is None:
        raise HTTPException(status_code=400, detail=f"Document type {parsed_type_id} not found.")
    parsed_received = _parse_optional_date(date_received, "date received")
    field_provenance = _collect_field_provenance(
        document_type_id=document_type_source,
        date_received=date_received_source,
        source=source_source,
        notes=notes_source,
    )

    try:
        result = promote_attachment_to_document(
            db,
            vault,
            attachment,
            document_type_id=parsed_type_id,
            source=source.strip() or None,
            notes=notes.strip() or None,
            date_received=parsed_received,
            field_provenance=field_provenance,
            actor=actor,
        )
    except AttachmentNotPromotableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()

    if not result.created_new_document:
        message = "This document is already in FERChronos."
    elif result.already_linked:
        message = "This attachment was already added to Documents."
    else:
        message = "Added to Documents."

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_attachment_review.html",
        _review_context(db, attachment, promotion_message=message, promoted_document=result.document),
    )


@router.post("/attachments/{attachment_id}/exclude")
def post_exclude_attachment(
    attachment_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    attachment = _get_attachment_or_404(db, attachment_id)
    exclude_attachment(db, attachment, actor)
    db.commit()
    return RedirectResponse(url=f"/communications/attachments/{attachment_id}/review", status_code=303)


@router.post("/attachments/{attachment_id}/leave-with-email")
def post_leave_attachment_with_email(
    attachment_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    attachment = _get_attachment_or_404(db, attachment_id)
    leave_attachment_with_email(db, attachment, actor)
    db.commit()
    return RedirectResponse(url=f"/communications/attachments/{attachment_id}/review", status_code=303)


# --- Communications search (Communications Phase Step 6) -------------------


@router.get("/search", response_class=HTMLResponse)
def search_communications_route(
    request: Request,
    q: str = Query(""),
    case_id: str = Query(""),
    sender: str = Query(""),
    recipient: str = Query(""),
    cc: str = Query(""),
    subject: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    has_attachments: str = Query(""),
    threaded: str = Query(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the Communications search form and, if any query/filter is
    given, its results. Independent of Document search
    (app/api/search.py) -- shares no query, no index, no route.
    """
    parsed_case_id = int(case_id) if case_id.strip() else None
    parsed_date_from = _parse_optional_date(date_from.strip(), "date_from")
    parsed_date_to = _parse_optional_date(date_to.strip(), "date_to")
    parsed_has_attachments = has_attachments if has_attachments in ("true", "false") else ""
    parsed_threaded = threaded if threaded in ("true", "false") else ""

    results = search_communications(
        db,
        query=q,
        case_id=parsed_case_id,
        sender=sender,
        recipient=recipient,
        cc=cc,
        subject=subject,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        has_attachments={"true": True, "false": False}.get(parsed_has_attachments),
        threaded={"true": True, "false": False}.get(parsed_threaded),
    )

    searched = bool(
        q.strip()
        or case_id.strip()
        or sender.strip()
        or recipient.strip()
        or cc.strip()
        or subject.strip()
        or date_from.strip()
        or date_to.strip()
        or parsed_has_attachments
        or parsed_threaded
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communications_search.html",
        {
            "query": q,
            "case_id": case_id,
            "sender": sender,
            "recipient": recipient,
            "cc": cc,
            "subject": subject,
            "date_from": date_from,
            "date_to": date_to,
            "selected_has_attachments": parsed_has_attachments,
            "selected_threaded": parsed_threaded,
            "cases": _list_cases(db),
            "results": results,
            "searched": searched,
        },
    )
