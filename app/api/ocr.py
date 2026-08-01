"""OCR job status routes.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. Genuinely new UI surface
-- nothing before Phase 3 was ever asynchronous, so there was never a
"jobs in progress" view to build until now. No OCR-execution or
correction routes exist yet -- those land in later Phase 3 steps.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import Case, Document, OcrJob

router = APIRouter(tags=["ocr"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    return case


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
