"""The OCR job queue's worker: claim, run, record, finish -- plus startup
crash recovery.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Steps 0-1. This is the app's
first background execution model -- every prior phase was entirely
synchronous, in-request. A single in-process worker (one job at a time;
no concurrency tuning needed for a single-user, single-machine tool)
pulls queued `ocr_jobs` rows FIFO by `queued_at`.

This module owns every job-lifecycle side effect -- claiming, finishing,
and the `ocr_completed`/`ocr_failed` custody event -- while
`app/core/ocr/service.py::run_ocr_job()` owns only the OCR content work
itself and returns a plain result. That split (rather than having
`run_ocr_job()` finish its own job) keeps this module import-free of a
cycle: `service.py` never needs to import anything from here.

Real OCR execution replaced Step 0's queue-mechanics placeholder in this
step -- the custody event now reflects a genuine action, not a fake one.
The actor recorded is a fixed, clearly-labeled system identity
(`SYSTEM_ACTOR`), never a human name -- there is no HTTP request or user
session behind a background job the way there is for `enqueue_ocr_job`,
which correctly still uses the uploading user's own actor name.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.custody import write_custody_event
from app.core.ocr.service import run_ocr_job
from app.core.vault import VaultLayout
from app.db.models import OcrJob

# Mirrors ocr_jobs.status's vocabulary -- see the OcrJob model docstring.
_ACTIVE_STATUSES = ("queued", "running")

DEFAULT_POLL_INTERVAL_SECONDS = 2.0

SYSTEM_ACTOR = "system (OCR background worker)"


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

    Any custody event for this outcome must be added to the session
    *before* calling this -- see process_next_job -- so it lands in the
    same commit as the status change, never a separate one that could be
    lost to a crash between the two.
    """
    job.status = status
    job.finished_at = datetime.now(timezone.utc)
    job.error = error
    job.document.ocr_status = status
    db.commit()


def process_next_job(db: Session, vault: VaultLayout) -> OcrJob | None:
    """Claim and run one job, if any is queued. Returns it, or None."""
    job = claim_next_job(db)
    if job is None:
        return None

    result = run_ocr_job(db, vault, job)

    write_custody_event(
        db,
        job.document,
        event_type="ocr_failed" if result.status == "failed" else "ocr_completed",
        actor=SYSTEM_ACTOR,
        details={
            "job_id": job.job_id,
            "pages_ocred": result.pages_ocred,
            "pages_failed": result.pages_failed,
        },
    )
    finish_job(db, job, status=result.status, error=result.error)
    return job


def sweep_stuck_jobs(db: Session) -> int:
    """Mark any job left `running` (an interrupted worker/app crash) `failed`.

    Run once at app startup, before serving requests -- see
    app/main.py::create_app. Any pages a real OCR run had already written
    before the crash are untouched -- run_ocr_job()/finish_job() commit
    together, so an interrupted run never leaves a half-written page
    committed; this only corrects the job's own status so it isn't
    silently stuck `running` forever with no way to observe or retry it.
    """
    stuck_jobs = db.scalars(select(OcrJob).where(OcrJob.status == "running")).all()
    for job in stuck_jobs:
        finish_job(
            db, job, status="failed", error="interrupted — application restarted mid-job"
        )
    return len(stuck_jobs)


def run_worker_loop(
    session_factory: sessionmaker[Session],
    vault: VaultLayout,
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
            job = process_next_job(db, vault)
        finally:
            db.close()

        if job is None:
            stop_event.wait(poll_interval_seconds)
