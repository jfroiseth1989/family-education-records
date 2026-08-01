"""Enqueueing a document for OCR.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. This module only ever
creates a `queued` `ocr_jobs` row and logs the real `ocr_queued` custody
event -- it does not process jobs (see app/jobs/worker.py) or run any
OCR itself.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.db.models import Document, OcrJob


def enqueue_ocr_job(db: Session, document: Document, actor: str) -> OcrJob:
    """Create a queued OCR job for `document` and log an `ocr_queued` event.

    Does not commit -- the caller controls the transaction boundary, same
    convention as every other core module (`ingest_document`,
    `extract_document`, `create_highlight`, ...).
    """
    job = OcrJob(document=document, status="queued")
    db.add(job)
    db.flush()  # assigns job.job_id

    document.ocr_status = "queued"

    write_custody_event(
        db,
        document,
        event_type="ocr_queued",
        actor=actor,
        details={"job_id": job.job_id},
    )
    return job
