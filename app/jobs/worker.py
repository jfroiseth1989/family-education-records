"""The OCR job queue's worker: claim, process, finish -- plus startup
crash recovery.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. This is the app's first
background execution model -- every prior phase was entirely synchronous,
in-request. A single in-process worker (one job at a time; no concurrency
tuning needed for a single-user, single-machine tool) pulls queued
`ocr_jobs` rows FIFO by `queued_at`.

**Step 0 placeholder note:** real OCR execution (`app/core/ocr/service.py`,
Tesseract, `document_pages.ocr_text` writes) does not exist yet -- that is
Step 1. `_run_step0_placeholder` below exists solely to prove the queue
mechanics (claim, run, finish, crash recovery) work correctly before any
real OCR code exists. It does not touch `document_pages` and does not log
an `ocr_completed`/`ocr_failed` custody event -- doing so would misrepresent
a fake, no-op run as a real OCR action. Only the genuine `ocr_queued` event
(logged at enqueue time, see app/core/ocr/queue.py) exists in Step 0.
Step 1 replaces the call site in `process_next_job` with real execution
and adds the corresponding custody events at that point.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OcrJob

# Mirrors ocr_jobs.status's vocabulary -- see the OcrJob model docstring.
_ACTIVE_STATUSES = ("queued", "running")

DEFAULT_POLL_INTERVAL_SECONDS = 2.0


def claim_next_job(db: Session) -> OcrJob | None:
    """Atomically claim the oldest queued job, if one exists.

    Commits immediately after marking the job `running` -- this durable
    write is what makes crash recovery (`sweep_stuck_jobs`) meaningful: a
    job a worker was actually processing when the process died is
    observably `running`, not still `queued` (never picked up) or
    silently lost.
    """
    job = db.scalars(
        select(OcrJob).where(OcrJob.status == "queued").order_by(OcrJob.queued_at, OcrJob.job_id)
    ).first()
    if job is None:
        return None

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.document.ocr_status = "running"
    db.commit()
    return job


def finish_job(db: Session, job: OcrJob, *, status: str, error: str | None = None) -> None:
    """Record a job's final status and commit.

    No custody event is written here in Step 0 -- see the module
    docstring. Step 1 adds `ocr_completed`/`ocr_failed`/
    `completed_with_errors` custody logging alongside real execution.
    """
    job.status = status
    job.finished_at = datetime.now(timezone.utc)
    job.error = error
    job.document.ocr_status = status
    db.commit()


def _run_step0_placeholder(job: OcrJob) -> tuple[str, str | None]:
    """Temporary stand-in for real OCR execution -- see module docstring.

    Does nothing but prove a job can be "processed" end-to-end. Replaced
    by real Tesseract execution in Step 1.
    """
    job.engine = "phase3-step0-placeholder"
    return "completed", None


def process_next_job(db: Session) -> OcrJob | None:
    """Claim and process one job, if any is queued. Returns it, or None."""
    job = claim_next_job(db)
    if job is None:
        return None

    status, error = _run_step0_placeholder(job)
    finish_job(db, job, status=status, error=error)
    return job


def sweep_stuck_jobs(db: Session) -> int:
    """Mark any job left `running` (an interrupted worker/app crash) `failed`.

    Run once at app startup, before serving requests -- see
    app/main.py::create_app. Any pages a real OCR run had already written
    before the crash are untouched (Step 1+); this only corrects the
    job's own status so it isn't silently stuck `running` forever with no
    way to observe or retry it.
    """
    stuck_jobs = db.scalars(select(OcrJob).where(OcrJob.status == "running")).all()
    for job in stuck_jobs:
        finish_job(
            db, job, status="failed", error="interrupted — application restarted mid-job"
        )
    return len(stuck_jobs)


def run_worker_loop(
    session_factory: sessionmaker[Session],
    stop_event: threading.Event,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """The background thread's main loop: process one job at a time, forever.

    Opens a fresh session per job (never shares a Session/connection
    across threads) and sleeps between polls when the queue is empty.
    Exits promptly once `stop_event` is set.
    """
    while not stop_event.is_set():
        db = session_factory()
        try:
            job = process_next_job(db)
        finally:
            db.close()

        if job is None:
            stop_event.wait(poll_interval_seconds)
