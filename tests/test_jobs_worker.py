"""Tests for app/jobs/worker.py -- claim/process/finish and crash recovery.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. `process_next_job` uses a
temporary placeholder in this step (no real OCR yet, see the module
docstring) -- these tests confirm the *queue mechanics* work correctly,
independent of what a job actually does.
"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.files import compute_sha256
from app.core.ingestion.service import ingest_document
from app.core.ocr.queue import enqueue_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, Document, OcrJob
from app.jobs.worker import claim_next_job, finish_job, process_next_job, sweep_stuck_jobs


def _queued_job(db, vault, case, tmp_path: Path, filename: str = "scan.jpg") -> OcrJob:
    image_path = tmp_path / filename
    # Content varies by filename so two calls in the same case never
    # collide with ingestion's byte-identical-content dedup rule.
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes for " + filename.encode())
    document = ingest_document(db, vault, case, image_path, filename, actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    job = enqueue_ocr_job(db, document, actor="test-user")
    db.commit()
    return job


# --- claim_next_job -----------------------------------------------------


def test_claim_next_job_returns_none_when_queue_empty(db_session: Session):
    assert claim_next_job(db_session) is None


def test_claim_next_job_claims_and_marks_running(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)

    claimed = claim_next_job(db_session)

    assert claimed.job_id == job.job_id
    assert claimed.status == "running"
    assert claimed.started_at is not None
    assert claimed.document.ocr_status == "running"


def test_claim_next_job_is_fifo_by_queued_at(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    first = _queued_job(db_session, vault, sample_case, tmp_path, "first.jpg")
    time.sleep(0.01)
    second = _queued_job(db_session, vault, sample_case, tmp_path, "second.jpg")

    claimed = claim_next_job(db_session)
    assert claimed.job_id == first.job_id

    # First job is now "running", not "queued" -- the next claim should
    # skip it and pick up the second job.
    claimed_again = claim_next_job(db_session)
    assert claimed_again.job_id == second.job_id


def test_claim_next_job_ignores_running_and_completed_jobs(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    claim_next_job(db_session)  # -> running
    assert claim_next_job(db_session) is None  # nothing else queued


# --- finish_job ----------------------------------------------------------


def test_finish_job_sets_status_and_finished_at(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    claimed = claim_next_job(db_session)

    finish_job(db_session, claimed, status="completed")

    assert claimed.status == "completed"
    assert claimed.finished_at is not None
    assert claimed.document.ocr_status == "completed"


def test_finish_job_records_error_on_failure(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    claimed = claim_next_job(db_session)

    finish_job(db_session, claimed, status="failed", error="something went wrong")

    assert claimed.status == "failed"
    assert claimed.error == "something went wrong"
    assert claimed.document.ocr_status == "failed"


# --- process_next_job (Step 0 placeholder) --------------------------------


def test_process_next_job_processes_one_job_end_to_end(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)

    processed = process_next_job(db_session)

    assert processed.job_id == job.job_id
    assert processed.status == "completed"
    assert processed.engine == "phase3-step0-placeholder"
    assert processed.document.ocr_status == "completed"


def test_process_next_job_returns_none_when_queue_empty(db_session: Session):
    assert process_next_job(db_session) is None


def test_process_next_job_does_not_write_a_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """The Step 0 placeholder does no real OCR -- writing ocr_completed for
    a fake run would misrepresent it as a genuine action. Only the
    ocr_queued event (from enqueue_ocr_job) should exist.
    """
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document

    process_next_job(db_session)

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "ocr_queued"]


def test_process_next_job_never_touches_document_pages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document
    pages_before = [(p.page_id, p.ocr_text, p.extracted_text) for p in document.pages]

    process_next_job(db_session)

    pages_after = [(p.page_id, p.ocr_text, p.extracted_text) for p in document.pages]
    assert pages_after == pages_before


def test_process_next_job_never_modifies_the_stored_original(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document
    stored_path = vault.root / document.stored_path
    hash_before = compute_sha256(stored_path)
    content_before = stored_path.read_bytes()

    process_next_job(db_session)

    assert compute_sha256(stored_path) == hash_before
    assert stored_path.read_bytes() == content_before


# --- sweep_stuck_jobs ------------------------------------------------


def test_sweep_stuck_jobs_marks_running_jobs_failed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    claim_next_job(db_session)  # simulate: worker claimed it, then crashed
    assert job.status == "running"

    count = sweep_stuck_jobs(db_session)

    assert count == 1
    assert job.status == "failed"
    assert "interrupted" in job.error
    assert job.document.ocr_status == "failed"


def test_sweep_stuck_jobs_ignores_queued_jobs(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)

    count = sweep_stuck_jobs(db_session)

    assert count == 0
    assert job.status == "queued"


def test_sweep_stuck_jobs_ignores_completed_jobs(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _queued_job(db_session, vault, sample_case, tmp_path)
    process_next_job(db_session)  # -> completed

    count = sweep_stuck_jobs(db_session)

    assert count == 0


def test_sweep_stuck_jobs_returns_zero_when_nothing_stuck(db_session: Session):
    assert sweep_stuck_jobs(db_session) == 0
