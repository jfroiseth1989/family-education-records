"""OCR job status, review, correction, and reprocess routes.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Steps 0 and 4.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db, get_vault
from app.core.extraction.dispatcher import is_image_extension
from app.core.ocr.corrections import create_ocr_correction
from app.core.ocr.queue import enqueue_ocr_job
from app.core.ocr.service import render_pdf_page_to_png_bytes
from app.core.ocr.text import effective_text
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentPage, OcrJob

router = APIRouter(tags=["ocr"])

# Jobs in these statuses mean OCR is already in flight for a document --
# a reprocess request while one is active would just queue a second,
# redundant job (app/jobs/worker.py processes one job at a time anyway,
# but the resulting duplicate ocr_jobs row and custody event would be
# confusing noise, not a real second run).
_ACTIVE_JOB_STATUSES = ("queued", "running")


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


def _get_page_or_404(db: Session, document_id: int, page_number: int) -> DocumentPage:
    page = db.scalar(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )
    if page is None:
        raise HTTPException(
            status_code=404, detail=f"Page {page_number} not found for document {document_id}."
        )
    return page


@router.get("/documents/{document_id}/pages/{page_number}/image")
def get_document_page_image(
    document_id: int,
    page_number: int,
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
):
    """Serve a read-only rendering of one page, for the OCR review UI.

    A standalone image document (one page) is served directly from the
    stored original. A PDF page is rendered to PNG bytes via PyMuPDF --
    the original file itself is only ever opened read-only, never
    modified (docs/PHASE_3_IMPLEMENTATION_PLAN.md standing rules).
    """
    document = _get_document_or_404(db, document_id)
    _get_page_or_404(db, document_id, page_number)

    full_path = vault.root / document.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Stored file is missing from the vault.")

    if is_image_extension(document.original_filename):
        return FileResponse(path=full_path)

    png_bytes = render_pdf_page_to_png_bytes(full_path, page_number)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/documents/{document_id}/ocr-review", response_class=HTMLResponse)
def review_document_ocr(
    request: Request, document_id: int, page: int | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the OCR review UI for one `needs_ocr` page at a time.

    Page navigation is restricted to pages actually flagged `needs_ocr`
    (docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 4) -- a natively-extracted
    page has nothing to review here. Shows the rendered page image next
    to `effective_text()`'s current resolution, that page's full
    correction history (oldest first), and a reprocess form.
    """
    document = _get_document_or_404(db, document_id)

    review_pages = sorted(
        (p for p in document.pages if p.needs_ocr), key=lambda p: p.page_number
    )
    if not review_pages:
        raise HTTPException(status_code=404, detail="This document has no pages needing OCR review.")

    page_numbers = [p.page_number for p in review_pages]
    current_number = page if page is not None and page in page_numbers else page_numbers[0]
    current_page = next(p for p in review_pages if p.page_number == current_number)

    current = effective_text(current_page)
    reprocess_active = document.ocr_status in _ACTIVE_JOB_STATUSES

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "ocr_review.html",
        {
            "document": document,
            "page_numbers": page_numbers,
            "current_page": current_page,
            "current_number": current_number,
            "effective": current,
            "corrections": current_page.corrections,
            "has_any_corrections": any(p.corrections for p in review_pages),
            "reprocess_active": reprocess_active,
        },
    )


@router.post("/documents/{document_id}/ocr-review/pages/{page_number}/correct")
def correct_document_ocr_page(
    document_id: int,
    page_number: int,
    corrected_text: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    _get_document_or_404(db, document_id)
    page = _get_page_or_404(db, document_id, page_number)

    try:
        create_ocr_correction(db, page, corrected_text, actor=actor)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(
        url=f"/documents/{document_id}/ocr-review?page={page_number}", status_code=303
    )


@router.post("/documents/{document_id}/ocr-review/reprocess")
def reprocess_document_ocr(
    document_id: int,
    page: int | None = None,
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Re-queue this document for OCR (docs/PHASE_3_DECISIONS.md §7: manual only).

    Refuses if a job for this document is already queued or running,
    rather than silently piling up a redundant one.
    """
    document = _get_document_or_404(db, document_id)

    if document.ocr_status in _ACTIVE_JOB_STATUSES:
        raise HTTPException(status_code=400, detail="OCR is already queued or running for this document.")

    enqueue_ocr_job(db, document, actor=actor)
    db.commit()
    redirect_url = f"/documents/{document_id}/ocr-review"
    if page is not None:
        redirect_url += f"?page={page}"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/cases/{case_id}/ocr-jobs", response_class=HTMLResponse)
def list_case_ocr_jobs(request: Request, case_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render every OCR job for documents in this case, most recent first."""
    case = _get_case_or_404(db, case_id)

    jobs = db.scalars(
        select(OcrJob)
        .join(Document, OcrJob.document_id == Document.document_id)
        .where(Document.case_id == case_id)
        .order_by(OcrJob.queued_at.desc())
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "ocr_jobs.html",
        {"case": case, "jobs": jobs},
    )
