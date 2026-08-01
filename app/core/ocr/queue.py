"""Enqueueing a document for OCR.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. This module only ever
creates a `queued` `ocr_jobs` row and logs the real `ocr_queued`/
`ocr_reprocessed` custody event -- it does not process jobs (see
app/jobs/worker.py) or run any OCR itself.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.db.models import Document, OcrJob

# A document's ocr_status is None only ever before its very first OCR
# enqueue -- any other value (completed / completed_with_errors / failed;
# queued / running are excluded by the caller's own duplicate-job guard,
# see app/api/ocr.py) means this document has already been through OCR at
# least once, so a new enqueue is a *reprocess*, not a first run. Mirrors
# extract_document()'s `is_reextraction = extraction_status != "pending"`
# check exactly (Phase 2 Step 1).
_ALREADY_OCRD_STATUSES = ("completed", "completed_with_errors", "failed")


def enqueue_ocr_job(db: Session, document: Document, actor: str) -> OcrJob:
    """Create a queued OCR job for `document` and log the custody event.

    Logs `ocr_reprocessed` instead of `ocr_queued` when this document has
    already been through an OCR run before (Phase 3 Step 4) -- same
    first-run-vs-rerun distinction extraction's `extracted`/`re_extracted`
    events already make. Does not commit -- the caller controls the
    transaction boundary, same convention as every other core module
    (`ingest_document`, `extract_document`, `create_highlight`, ...).
    """
    is_reprocess = document.ocr_status in _ALREADY_OCRD_STATUSES

    job = OcrJob(document=document, status="queued")
    db.add(job)
    db.flush()  # assigns job.job_id

    document.ocr_status = "queued"

    write_custody_event(
        db,
        document,
        event_type="ocr_reprocessed" if is_reprocess else "ocr_queued",
        actor=actor,
        details={"job_id": job.job_id},
    )
    return job
