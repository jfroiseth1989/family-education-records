"""Document routes: upload (ingest), view, integrity check, version linking.

See docs/ARCHITECTURE.md §3.1 (ingestion) and §3.2 (versioning / custody).
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db, get_vault
from app.core.annotations.service import count_document_annotations
from app.core.custody import verify_document_integrity
from app.core.document_dates import InvalidDateRangeError
from app.core.extraction.service import extract_document
from app.core.ingestion.service import DuplicateDocumentError, ingest_document
from app.core.ingestion.versioning import VersionLinkError, link_as_new_version
from app.core.ocr.queue import enqueue_ocr_job
from app.core.tagging import list_case_tags
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentDatePrecision

router = APIRouter(tags=["documents"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    return case


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


def _maybe_enqueue_ocr(db: Session, document: Document, actor: str) -> None:
    """Queue `document` for OCR if extraction flagged any page as needing it.

    Runs right after `extract_document()`, in the same request — enqueuing
    itself is cheap (one row, no OCR execution), unlike OCR itself, which
    runs in the background (see app/jobs/worker.py).
    """
    if document.needs_ocr:
        enqueue_ocr_job(db, document, actor)


def _save_upload_to_temp(upload: UploadFile) -> Path:
    """Spool an uploaded file to a temp path so ingestion can hash/copy it.

    The temp file is a working copy of what the browser sent — it is not
    the "original" in the chain-of-custody sense; the copy ingestion makes
    into `originals/` from this temp file is. Callers are responsible for
    deleting the temp file once ingestion has copied it into the vault.
    """
    suffix = Path(upload.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(upload.file, tmp)
        return Path(tmp.name)


def _parse_document_date_form(
    document_date_raw: str, precision_raw: str, range_end_raw: str
) -> tuple[date | None, DocumentDatePrecision, date | None]:
    """Parse the ingestion form's date fields: date, precision, range end.

    Each of the three fields is validated independently here (format only
    -- an empty date/range-end means "unknown/not applicable," a fully
    valid choice, and an unparseable non-empty value or unrecognized
    precision is rejected with a 400 rather than silently ignored).
    Cross-field validation (e.g. "range" precision requires a range end,
    exact/approximate must not have one, range end must not precede the
    start) happens later, inside `ingest_document` via
    `app.core.document_dates.validate_date_combination`, and is surfaced
    to the caller as `InvalidDateRangeError` -> 400 (see both routes below).
    """
    try:
        precision = DocumentDatePrecision(precision_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid document date precision '{precision_raw}'.",
        ) from exc

    def _parse_optional_date(raw: str, field_label: str) -> date | None:
        value = raw.strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_label} '{value}' (expected YYYY-MM-DD).",
            ) from exc

    parsed_date = _parse_optional_date(document_date_raw, "document date")
    parsed_range_end = _parse_optional_date(range_end_raw, "document date range end")

    return parsed_date, precision, parsed_range_end


@router.post("/cases/{case_id}/documents")
def upload_document(
    request: Request,
    case_id: int,
    file: UploadFile,
    source: str = Form(""),
    document_type_id: str = Form(""),
    notes: str = Form(""),
    document_date: str = Form(""),
    document_date_precision: str = Form("exact"),
    document_date_range_end: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Ingest an uploaded file as a new document in the given case."""
    case = _get_case_or_404(db, case_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    parsed_type_id = int(document_type_id) if document_type_id.strip() else None
    parsed_date, date_precision, parsed_range_end = _parse_document_date_form(
        document_date, document_date_precision, document_date_range_end
    )

    temp_path = _save_upload_to_temp(file)
    try:
        try:
            document = ingest_document(
                db,
                vault,
                case,
                source_file_path=temp_path,
                original_filename=file.filename,
                actor=actor,
                source=source.strip() or None,
                document_type_id=parsed_type_id,
                notes=notes.strip() or None,
                mime_type=file.content_type,
                document_date=parsed_date,
                document_date_precision=date_precision,
                document_date_range_end=parsed_range_end,
            )
        except DuplicateDocumentError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidDateRangeError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Extraction runs in the same request, right after a successful
        # ingest, but never raises out to here -- an unparseable or
        # unsupported file is recorded via extraction_status/extraction_error
        # on the document itself (approved decision 6), not an exception.
        extract_document(db, vault, document, actor)
        _maybe_enqueue_ocr(db, document, actor)

        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.get("/documents/{document_id}", response_class=HTMLResponse)
def get_document(
    request: Request, document_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render a document's detail page: metadata, custody log, and version history."""
    document = _get_document_or_404(db, document_id)

    version_siblings = []
    if document.version_group_id is not None:
        version_siblings = sorted(
            db.scalars(
                select(Document).where(Document.version_group_id == document.version_group_id)
            ).all(),
            key=lambda d: d.version_number or 0,
        )

    other_case_documents = sorted(
        (
            d
            for d in document.case.documents
            if d.document_id != document.document_id
            and d.is_current_version
            and d.version_group_id != document.version_group_id
        ),
        key=lambda d: d.original_filename.lower(),
    )

    document_tags = sorted((link.tag for link in document.tag_links), key=lambda t: t.name.lower())
    case_tags = list_case_tags(db, document.case_id)
    annotation_counts = count_document_annotations(db, document.document_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "document_detail.html",
        {
            "document": document,
            "custody_events": document.custody_events,
            "version_siblings": version_siblings,
            "other_case_documents": other_case_documents,
            "pages": sorted(document.pages, key=lambda p: p.page_number),
            "document_tags": document_tags,
            "case_tags": case_tags,
            "annotation_counts": annotation_counts,
        },
    )


@router.get("/documents/{document_id}/file")
def download_document_file(
    document_id: int,
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
):
    """Serve the stored original file, read-only, for viewing/downloading."""
    document = _get_document_or_404(db, document_id)
    full_path = vault.root / document.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Stored file is missing from the vault.")
    return FileResponse(
        path=full_path,
        filename=document.original_filename,
        media_type=document.mime_type or "application/octet-stream",
    )


@router.post("/documents/{document_id}/verify")
def verify_document(
    document_id: int,
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Re-verify a document's stored file against its recorded SHA-256 hash.

    Always logs a `hash_verified` custody event (see
    app/core/custody.py::verify_document_integrity) whether or not the hash
    still matches, so the result is visible in the custody log either way.
    """
    document = _get_document_or_404(db, document_id)
    verify_document_integrity(db, vault, document, actor)
    db.commit()
    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.post("/documents/{document_id}/new-version")
def upload_new_version(
    request: Request,
    document_id: int,
    file: UploadFile,
    version_note: str = Form(""),
    document_date: str = Form(""),
    document_date_precision: str = Form("exact"),
    document_date_range_end: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Ingest an uploaded file and link it as the new current version of `document_id`.

    Combines ingestion and version-linking into one step: the existing
    document is never modified, and the new file becomes its own
    independent document row before being linked — see
    app/core/ingestion/versioning.py.

    The new version's document date is entered fresh here, not copied from
    the prior version: a reissued or corrected record often carries a new
    effective date (e.g. the date it was reissued), so silently inheriting
    the old one could record the wrong date rather than an honestly
    "unknown" one.
    """
    existing_document = _get_document_or_404(db, document_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    parsed_date, date_precision, parsed_range_end = _parse_document_date_form(
        document_date, document_date_precision, document_date_range_end
    )

    temp_path = _save_upload_to_temp(file)
    try:
        try:
            new_document = ingest_document(
                db,
                vault,
                existing_document.case,
                source_file_path=temp_path,
                original_filename=file.filename,
                actor=actor,
                source=existing_document.source,
                document_type_id=existing_document.document_type_id,
                mime_type=file.content_type,
                document_date=parsed_date,
                document_date_precision=date_precision,
                document_date_range_end=parsed_range_end,
            )
        except DuplicateDocumentError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidDateRangeError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        extract_document(db, vault, new_document, actor)
        _maybe_enqueue_ocr(db, new_document, actor)

        try:
            link_as_new_version(
                db,
                existing_document=existing_document,
                new_document=new_document,
                actor=actor,
                version_note=version_note.strip() or None,
            )
        except VersionLinkError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return RedirectResponse(url=f"/documents/{new_document.document_id}", status_code=303)
