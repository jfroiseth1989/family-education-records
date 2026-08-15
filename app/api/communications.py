"""Communications routes (Communications Phase Steps 2-9): Yahoo account
connect/disconnect, manual .eml/.mbox upload/viewing, email thread views,
attachment review/promotion to Documents, Communications search, and
(Step 9) read-only Yahoo IMAP mailbox browsing/searching.

Manual upload is deliberately independent of any connected mailbox: it
never reads `CommunicationAccount`, never checks connection status, and
works identically whether zero or several Yahoo accounts are connected.
Thread reconstruction (`app/core/communications/thread_rebuild.py`) runs
automatically inside `import_eml_file()`; these routes only ever read the
resulting `communication_threads` rows, never write to them.

The Step 9 mailbox-browsing routes below (`test-connection`, `browse`)
are FERChronos's only outbound network calls, and only ever run when a
human explicitly requests them (a form submit or a followed link) --
see docs/PRIVACY_SECURITY.md's "Outbound network exception" section.
They never write a `Communication`/`CommunicationAttachment` row, never
write to the vault, and never write a custody event -- Step 9 is
inspection only; importing what a search finds is Step 10's job, not
built here.
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
from app.core.communications.credentials import KeyringUnavailableError, get_credential
from app.core.communications.imap_client import (
    DEFAULT_SEARCH_LIMIT,
    ImapAuthenticationError,
    ImapCredentialUnavailableError,
    ImapError,
    ImapFolder,
    ImapMailboxAccessError,
    ImapNetworkError,
    ImapSearchCriteria,
    ImapTimeoutError,
)
from app.core.communications.bulk_attachment_review import (
    AttachmentReviewFilters,
    bulk_add_to_documents,
    bulk_exclude,
    bulk_leave_with_email,
    list_recognized_pending_attachments,
    list_review_attachments,
    preview_bulk_promotion,
)
from app.core.communications.imap_service import open_connection
from app.core.communications.import_batches import (
    BatchValidationError,
    count_remaining,
    create_batch,
    request_cancel,
    resume_batch,
    retry_failed_items,
)
from app.core.communications.ingestion import DuplicateCommunicationError, import_eml_file
from app.core.communications.mbox_import import import_mbox_file
from app.core.communications.promotion import (
    AttachmentNotPromotableError,
    exclude_attachment,
    leave_attachment_with_email,
    promote_attachment_to_document,
)
from app.core.facts.service import CONFIDENCE_LABELS
from app.core.indexing.communications_search import search_communications
from app.core.vault import VaultLayout
from app.db.models import (
    AiObservation,
    Case,
    Communication,
    CommunicationAccount,
    CommunicationAttachment,
    CommunicationDocumentLink,
    CommunicationImportBatch,
    CommunicationImportBatchItem,
    CommunicationThread,
    DocumentType,
    VerifiedFact,
)

router = APIRouter(prefix="/communications", tags=["communications"])


def _list_accounts(db: Session) -> list[CommunicationAccount]:
    return list(
        db.scalars(
            select(CommunicationAccount).order_by(CommunicationAccount.connected_at.desc())
        ).all()
    )


def _account_rows(db: Session) -> list[dict]:
    """Each account paired with whether its credential is actually
    present in the OS keyring right now (Step 12) -- a local,
    network-free lookup (`get_credential()` never contacts Yahoo), so
    this is safe to compute on every page load. A `connected` account
    whose keyring entry was deleted or revoked outside FERChronos (the
    OS store was cleared, a sync tool wiped it, etc.) shows as
    "Credential unavailable" here rather than a misleading "connected" --
    the database's own `status` column has no way to know this on its
    own, since deleting a keyring entry is invisible to it until
    something actually tries to use the credential.
    """
    rows = []
    for account in _list_accounts(db):
        credential_available = (
            account.status == "connected" and get_credential(account.credential_ref) is not None
        )
        rows.append(
            {
                "account": account,
                "credential_available": credential_available,
            }
        )
    return rows


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


def _list_recent_import_batches(db: Session, limit: int = 20) -> list[CommunicationImportBatch]:
    return list(
        db.scalars(
            select(CommunicationImportBatch).order_by(CommunicationImportBatch.created_at.desc()).limit(limit)
        ).all()
    )


def _home_context(
    db: Session,
    *,
    connect_error: str | None = None,
    upload_error: str | None = None,
    mbox_error: str | None = None,
    mbox_message: str | None = None,
    mailbox_test_account_id: int | None = None,
    mailbox_test_message: str | None = None,
    mailbox_test_ok: bool | None = None,
) -> dict:
    return {
        "account_rows": _account_rows(db),
        "connect_error": connect_error,
        "communications": _list_recent_communications(db),
        "cases": _list_cases(db),
        "upload_error": upload_error,
        "mbox_error": mbox_error,
        "mbox_message": mbox_message,
        "mailbox_test_account_id": mailbox_test_account_id,
        "mailbox_test_message": mailbox_test_message,
        "mailbox_test_ok": mailbox_test_ok,
        "import_batches": _list_recent_import_batches(db),
    }


# Every message here is a fixed, safe string -- never built from a raw
# server response or from any credential -- see imap_client.py's module
# docstring. (ImapError itself is the fallback for the one unnamed
# base-class case: `_require_connected()`'s "not connected" -- unreachable
# from any route below, since every route always connects first, but
# handled to satisfy "never let a raw exception reach the user" anyway.)
_IMAP_ERROR_MESSAGES: tuple[tuple[type[ImapError], str], ...] = (
    (
        ImapCredentialUnavailableError,
        "No secure credential is currently stored for this account. Reconnect this Yahoo account to continue.",
    ),
    (ImapAuthenticationError, "Yahoo rejected the app password for this account. Reconnect with a fresh app password."),
    (ImapTimeoutError, "The mail server did not respond in time. Try again in a moment."),
    (ImapNetworkError, "Could not reach Yahoo's mail server. Check your network connection and try again."),
    (ImapMailboxAccessError, "Yahoo's mail server could not complete this request."),
)


def _safe_imap_message(exc: ImapError) -> str:
    for error_type, message in _IMAP_ERROR_MESSAGES:
        if isinstance(exc, error_type):
            return message
    return "Could not complete this mailbox request."


def _imap_status_code(exc: ImapError) -> int:
    if isinstance(exc, ImapCredentialUnavailableError):
        return 400
    if isinstance(exc, ImapAuthenticationError):
        return 401
    if isinstance(exc, ImapTimeoutError):
        return 504
    if isinstance(exc, ImapNetworkError):
        return 503
    return 502


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


def _get_connected_account_or_404(db: Session, account_id: int) -> CommunicationAccount:
    account = db.get(CommunicationAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")
    if account.status != "connected":
        raise HTTPException(status_code=400, detail="This account is disconnected.")
    return account


@router.post("/{account_id}/test-connection", response_class=HTMLResponse)
def post_test_mailbox_connection(
    account_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Explicitly, on the user's request only, open one read-only IMAP
    connection to prove the stored app password still works and count
    the account's folders -- then immediately log out. Never reads a
    message, never writes anything to `communications`, the vault, or
    any custody ledger. A failed test never touches the stored account
    row -- see `_get_connected_account_or_404`/`open_connection`'s
    docstrings; a bad app password, an unreachable network, or a
    disconnected keyring entry all leave `communication_accounts`
    completely unchanged.
    """
    account = _get_connected_account_or_404(db, account_id)

    templates = request.app.state.templates
    try:
        client = open_connection(account)
        try:
            folder_count = len(client.list_folders())
        finally:
            client.logout()
    except ImapError as exc:
        return templates.TemplateResponse(
            request,
            "communications_home.html",
            _home_context(
                db,
                mailbox_test_account_id=account_id,
                mailbox_test_ok=False,
                mailbox_test_message=_safe_imap_message(exc),
            ),
            status_code=_imap_status_code(exc),
        )

    return templates.TemplateResponse(
        request,
        "communications_home.html",
        _home_context(
            db,
            mailbox_test_account_id=account_id,
            mailbox_test_ok=True,
            mailbox_test_message=f"Connected successfully. Found {folder_count} folder(s).",
        ),
    )


def _browse_context(
    db: Session,
    account: CommunicationAccount,
    *,
    folders: list[ImapFolder] | None,
    criteria: ImapSearchCriteria | None,
    result=None,
    error: str | None = None,
) -> dict:
    return {
        "account": account,
        "folders": folders or [],
        "criteria": criteria,
        "result": result,
        "error": error,
        "default_limit": DEFAULT_SEARCH_LIMIT,
        "cases": _list_cases(db),
    }


@router.get("/{account_id}/browse", response_class=HTMLResponse)
def get_browse_mailbox(
    account_id: int,
    request: Request,
    folder: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    sender: str = Query(""),
    recipient: str = Query(""),
    cc: str = Query(""),
    subject: str = Query(""),
    keywords: str = Query(""),
    offset: int = Query(0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Read-only Yahoo mailbox browsing/searching (Communications Phase
    Step 9) -- inspection only. Loading this page is itself the one
    explicit user action that opens a live IMAP connection: it always
    lists the account's real folders (so the folder selector reflects
    this specific mailbox, never a hard-coded Inbox/Sent guess), and
    additionally runs a search when a folder has been chosen.

    Every result shown here is metadata/preview only, fetched with
    `BODY.PEEK` so nothing is marked read on Yahoo's server -- nothing
    from this route is ever written to `communications`,
    `communication_attachments`, the vault, or any custody ledger. See
    the module docstring.
    """
    account = _get_connected_account_or_404(db, account_id)
    templates = request.app.state.templates

    try:
        client = open_connection(account)
    except ImapError as exc:
        return templates.TemplateResponse(
            request,
            "communication_browse.html",
            _browse_context(db, account, folders=None, criteria=None, error=_safe_imap_message(exc)),
            status_code=_imap_status_code(exc),
        )

    try:
        try:
            folders = client.list_folders()
        except ImapError as exc:
            return templates.TemplateResponse(
                request,
                "communication_browse.html",
                _browse_context(db, account, folders=None, criteria=None, error=_safe_imap_message(exc)),
                status_code=_imap_status_code(exc),
            )

        if not folder.strip():
            return templates.TemplateResponse(
                request, "communication_browse.html", _browse_context(db, account, folders=folders, criteria=None)
            )

        criteria = ImapSearchCriteria(
            folder=folder.strip(),
            date_from=_parse_optional_date(date_from.strip(), "date_from"),
            date_to=_parse_optional_date(date_to.strip(), "date_to"),
            sender=sender,
            recipient=recipient,
            cc=cc,
            subject=subject,
            keywords=keywords,
            limit=DEFAULT_SEARCH_LIMIT,
            offset=max(0, offset),
        )
        try:
            result = client.search(criteria)
        except ImapError as exc:
            return templates.TemplateResponse(
                request,
                "communication_browse.html",
                _browse_context(db, account, folders=folders, criteria=criteria, error=_safe_imap_message(exc)),
                status_code=_imap_status_code(exc),
            )

        return templates.TemplateResponse(
            request,
            "communication_browse.html",
            _browse_context(db, account, folders=folders, criteria=criteria, result=result),
        )
    finally:
        client.logout()


# --- bulk import batches (Communications Phase Step 10) ---------------------


@router.post("/{account_id}/import-batches")
def post_create_import_batch(
    account_id: int,
    request: Request,
    folder: str = Form(""),
    uid: list[str] = Form([]),
    case_id: str = Form(""),
    sender: str = Form(""),
    recipient: str = Form(""),
    cc: str = Form(""),
    subject: str = Form(""),
    keywords: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
):
    """Create a bulk-import batch from a set of messages selected on the
    browse/search page and immediately press "Start Import." Creating
    the batch is the one explicit user action Step 10 requires --
    actually fetching any message from Yahoo happens afterward, in the
    background worker (app/jobs/import_worker.py), driven only by the
    batch this route writes, never by this request itself opening an
    IMAP connection. No `Communication` row exists yet when this route
    returns.
    """
    account = _get_connected_account_or_404(db, account_id)

    if not case_id.strip() or not case_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="A student must be selected.")
    case = db.get(Case, int(case_id))
    if case is None:
        raise HTTPException(status_code=400, detail="Selected student was not found.")

    if not folder.strip():
        raise HTTPException(status_code=400, detail="No folder was selected.")

    search_criteria = {
        "folder": folder,
        "sender": sender,
        "recipient": recipient,
        "cc": cc,
        "subject": subject,
        "keywords": keywords,
        "date_from": date_from,
        "date_to": date_to,
    }

    try:
        batch = create_batch(
            db,
            account,
            case,
            items=[(folder, u) for u in uid],
            search_criteria=search_criteria,
            actor=actor,
        )
    except BatchValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/communications/import-batches/{batch.batch_id}", status_code=303)


def _get_batch_or_404(db: Session, batch_id: int) -> CommunicationImportBatch:
    batch = db.get(CommunicationImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Import batch {batch_id} not found.")
    return batch


@router.get("/import-batches/{batch_id}", response_class=HTMLResponse)
def get_import_batch_status(batch_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """A plain, manually-refreshed status page -- deliberately no
    auto-refresh/polling JavaScript, matching the OCR jobs page's own
    precedent and Step 10's "no background polling" requirement in
    spirit as well as letter.
    """
    batch = _get_batch_or_404(db, batch_id)
    items = list(
        db.scalars(
            select(CommunicationImportBatchItem)
            .where(CommunicationImportBatchItem.batch_id == batch_id)
            .order_by(CommunicationImportBatchItem.item_id)
        ).all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_import_batch.html",
        {
            "batch": batch,
            "items": items,
            "remaining": count_remaining(db, batch),
        },
    )


@router.post("/import-batches/{batch_id}/cancel")
def post_cancel_import_batch(batch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    batch = _get_batch_or_404(db, batch_id)
    request_cancel(db, batch)
    return RedirectResponse(url=f"/communications/import-batches/{batch_id}", status_code=303)


@router.post("/import-batches/{batch_id}/resume")
def post_resume_import_batch(batch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    batch = _get_batch_or_404(db, batch_id)
    resume_batch(db, batch)
    return RedirectResponse(url=f"/communications/import-batches/{batch_id}", status_code=303)


@router.post("/import-batches/{batch_id}/retry-failed")
def post_retry_failed_import_batch_items(batch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    batch = _get_batch_or_404(db, batch_id)
    retry_failed_items(db, batch)
    return RedirectResponse(url=f"/communications/import-batches/{batch_id}", status_code=303)


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


@router.post("/upload-mbox", response_class=HTMLResponse)
def post_upload_mbox(
    request: Request,
    file: UploadFile,
    case_id: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    """Manually import an `.mbox` archive for a chosen student, splitting
    it into its individual real messages -- see
    app/core/communications/mbox_import.py (Communications Phase Step 8).

    An archive can contain many messages, so this redirects back to the
    Communications home with a summary rather than to a single detail
    page. A message that duplicates one already imported (in this
    archive or a prior import) is skipped, not treated as a fatal error
    for the whole batch.
    """
    if not case_id.strip() or not case_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="A student must be selected.")
    case = db.get(Case, int(case_id))
    if case is None:
        raise HTTPException(status_code=400, detail="Selected student was not found.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="An .mbox file is required.")
    if Path(file.filename).suffix.lower() not in (".mbox", ".mbx"):
        raise HTTPException(status_code=400, detail="Only .mbox files are supported for archive import.")

    temp_path = _save_upload_to_temp(file)
    try:
        result = import_mbox_file(
            db,
            vault,
            case,
            source_file_path=temp_path,
            original_filename=file.filename,
            actor=actor,
        )
        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    parts = [f"{len(result.imported)} message(s) imported"]
    if result.duplicate_count:
        parts.append(f"{result.duplicate_count} duplicate(s) skipped")
    if result.unparseable_count:
        parts.append(f"{result.unparseable_count} empty/unparseable entr(ies) skipped")
    message = ", ".join(parts) + "."

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "communications_home.html", _home_context(db, mbox_message=message))


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

    # Communications Phase Step 7: at most one AiObservation ever exists
    # per communication (DB-enforced -- see the model's docstring), so a
    # single lookup is always the whole picture; its resulting
    # VerifiedFact (if accepted) is reached the same direct way.
    timeline_observation = db.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication_id)
    ).first()
    timeline_fact = None
    if timeline_observation is not None and timeline_observation.status == "accepted":
        timeline_fact = db.scalars(
            select(VerifiedFact).where(VerifiedFact.source_observation_id == timeline_observation.observation_id)
        ).first()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_detail.html",
        {
            "communication": communication,
            "attachments": communication.attachments,
            "document_links": document_links,
            "timeline_observation": timeline_observation,
            "timeline_fact": timeline_fact,
            "confidence_labels": sorted(CONFIDENCE_LABELS),
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


# --- bulk attachment review (Communications Phase Step 11) -----------------
#
# Everything below is orchestration only -- app/core/communications/
# bulk_attachment_review.py never classifies, never computes metadata
# suggestions, and never decides duplicate-vs-new-Document on its own; it
# calls the exact same Step 5 per-item functions
# (promote_attachment_to_document/exclude_attachment/
# leave_attachment_with_email) these routes' single-attachment
# counterparts above already use. No Yahoo/IMAP network call is possible
# from any route in this section -- everything needed was already made
# local by Step 10's import.


def _int_or_none(raw: str) -> int | None:
    return int(raw) if raw.strip().isdigit() else None


def _parse_review_filters(
    db: Session,
    *,
    batch_id: str,
    case_id: str,
    document_type_id: str,
    status: str,
    candidate: str,
    sender: str,
    subject: str,
    date_from: str,
    date_to: str,
) -> AttachmentReviewFilters:
    return AttachmentReviewFilters(
        batch_id=_int_or_none(batch_id),
        case_id=_int_or_none(case_id),
        document_type_id=_int_or_none(document_type_id),
        review_status=status,
        candidate_filter=candidate,
        sender=sender,
        subject=subject,
        date_from=_parse_optional_date(date_from.strip(), "date_from"),
        date_to=_parse_optional_date(date_to.strip(), "date_to"),
    )


def _resolve_attachments(db: Session, ids: list[str]) -> list[CommunicationAttachment]:
    resolved = []
    for raw in ids:
        parsed = _int_or_none(raw)
        if parsed is None:
            continue
        attachment = db.get(CommunicationAttachment, parsed)
        if attachment is not None:
            resolved.append(attachment)
    return resolved


def _bulk_review_context(
    db: Session,
    filters: AttachmentReviewFilters,
    *,
    bulk_result=None,
    add_all_preview=None,
) -> dict:
    rows = list_review_attachments(db, filters)
    document_types = list(
        db.scalars(select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)).all()
    )
    return {
        "rows": rows,
        "filters": filters,
        "cases": _list_cases(db),
        "document_types": document_types,
        "bulk_result": bulk_result,
        "add_all_preview": add_all_preview,
    }


@router.get("/attachments/review", response_class=HTMLResponse)
def get_bulk_attachment_review(
    request: Request,
    batch_id: str = Query(""),
    case_id: str = Query(""),
    document_type_id: str = Query(""),
    status: str = Query("pending"),
    candidate: str = Query(""),
    sender: str = Query(""),
    subject: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Bulk attachment review list -- defaults to every `pending`
    attachment across all accounts/students, narrowed by whichever
    filters are given. Read-only: never touches an attachment's review
    state, never contacts Yahoo.
    """
    filters = _parse_review_filters(
        db,
        batch_id=batch_id,
        case_id=case_id,
        document_type_id=document_type_id,
        status=status,
        candidate=candidate,
        sender=sender,
        subject=subject,
        date_from=date_from,
        date_to=date_to,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "communication_bulk_attachment_review.html", _bulk_review_context(db, filters)
    )


@router.post("/attachments/review/add-selected", response_class=HTMLResponse)
def post_bulk_add_selected(
    request: Request,
    attachment_id: list[str] = Form([]),
    batch_id: str = Form(""),
    case_id: str = Form(""),
    document_type_id: str = Form(""),
    status: str = Form("pending"),
    candidate: str = Form(""),
    sender: str = Form(""),
    subject: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    """Add every selected attachment to Documents, each independently --
    a duplicate is linked rather than failed, and one attachment's real
    failure never blocks or undoes any other. Uses each attachment's own
    Step 5 suggested metadata; per-item metadata edits happen on the
    existing single-attachment review page (linked from each row), not
    here.
    """
    filters = _parse_review_filters(
        db,
        batch_id=batch_id,
        case_id=case_id,
        document_type_id=document_type_id,
        status=status,
        candidate=candidate,
        sender=sender,
        subject=subject,
        date_from=date_from,
        date_to=date_to,
    )
    attachments = _resolve_attachments(db, attachment_id)
    summary = bulk_add_to_documents(db, vault, attachments, actor)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_bulk_attachment_review.html",
        _bulk_review_context(db, filters, bulk_result=summary),
    )


@router.post("/attachments/review/add-all-recognized", response_class=HTMLResponse)
def post_bulk_add_all_recognized(
    request: Request,
    confirmed: str = Form(""),
    batch_id: str = Form(""),
    case_id: str = Form(""),
    document_type_id: str = Form(""),
    status: str = Form("pending"),
    candidate: str = Form(""),
    sender: str = Form(""),
    subject: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    """"Add all recognized" -- scoped to `batch_id`/`case_id` (the two
    groupings Step 11 asks for), always meaning exactly "every `pending`
    attachment with an unambiguous classifier suggestion" regardless of
    whatever narrower display filters (sender/subject/date/suggested
    type) the page happened to be showing.

    Two-phase, and the candidate set is always recomputed fresh from the
    database rather than trusting any client-submitted attachment list --
    first submission (no `confirmed`) only *previews* counts, changing
    nothing; only a second submission with `confirmed=1` actually
    promotes anything. Recomputing fresh on the confirm step also makes
    resubmitting the same confirm safely idempotent: anything no longer
    `pending` (already handled by an earlier submission, or reviewed
    another way meanwhile) simply won't be in the candidate set again.
    """
    filters = _parse_review_filters(
        db,
        batch_id=batch_id,
        case_id=case_id,
        document_type_id=document_type_id,
        status=status,
        candidate=candidate,
        sender=sender,
        subject=subject,
        date_from=date_from,
        date_to=date_to,
    )
    candidates = list_recognized_pending_attachments(
        db, batch_id=_int_or_none(batch_id), case_id=_int_or_none(case_id)
    )
    templates = request.app.state.templates

    if confirmed != "1":
        preview = preview_bulk_promotion(db, candidates)
        return templates.TemplateResponse(
            request,
            "communication_bulk_attachment_review.html",
            _bulk_review_context(db, filters, add_all_preview=preview),
        )

    summary = bulk_add_to_documents(db, vault, candidates, actor)
    return templates.TemplateResponse(
        request,
        "communication_bulk_attachment_review.html",
        _bulk_review_context(db, filters, bulk_result=summary),
    )


@router.post("/attachments/review/exclude-selected", response_class=HTMLResponse)
def post_bulk_exclude_selected(
    request: Request,
    attachment_id: list[str] = Form([]),
    batch_id: str = Form(""),
    case_id: str = Form(""),
    document_type_id: str = Form(""),
    status: str = Form("pending"),
    candidate: str = Form(""),
    sender: str = Form(""),
    subject: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    filters = _parse_review_filters(
        db,
        batch_id=batch_id,
        case_id=case_id,
        document_type_id=document_type_id,
        status=status,
        candidate=candidate,
        sender=sender,
        subject=subject,
        date_from=date_from,
        date_to=date_to,
    )
    attachments = _resolve_attachments(db, attachment_id)
    summary = bulk_exclude(db, attachments, actor)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_bulk_attachment_review.html",
        _bulk_review_context(db, filters, bulk_result=summary),
    )


@router.post("/attachments/review/leave-selected", response_class=HTMLResponse)
def post_bulk_leave_selected(
    request: Request,
    attachment_id: list[str] = Form([]),
    batch_id: str = Form(""),
    case_id: str = Form(""),
    document_type_id: str = Form(""),
    status: str = Form("pending"),
    candidate: str = Form(""),
    sender: str = Form(""),
    subject: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> HTMLResponse:
    filters = _parse_review_filters(
        db,
        batch_id=batch_id,
        case_id=case_id,
        document_type_id=document_type_id,
        status=status,
        candidate=candidate,
        sender=sender,
        subject=subject,
        date_from=date_from,
        date_to=date_to,
    )
    attachments = _resolve_attachments(db, attachment_id)
    summary = bulk_leave_with_email(db, attachments, actor)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "communication_bulk_attachment_review.html",
        _bulk_review_context(db, filters, bulk_result=summary),
    )
