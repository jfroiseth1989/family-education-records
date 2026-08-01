"""Tests for app/jobs/worker.py -- claim/process/finish and crash recovery.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Steps 0-1. `process_next_job` now
runs real OCR execution (app/core/ocr/service.py) -- these tests mock at
the `pytesseract` call boundary (see tests/test_ocr_engine.py for that
seam) so they don't depend on a real Tesseract binary being installed.
Content-level OCR correctness is tested in tests/test_ocr_service.py;
these tests focus on the *queue* mechanics: claim, finish, crash recovery.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.files import compute_sha256
from app.core.ingestion.service import ingest_document
from app.core.ocr.engine import OcrResult
from app.core.ocr.queue import enqueue_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, OcrJob
from app.jobs.worker import claim_next_job, finish_job, process_next_job, sweep_stuck_jobs

_FAKE_RESULT = OcrResult(text="Mocked OCR output for worker tests.", confidence=88.0, word_boxes=[])


def _mock_ocr():
    """Patches the pytesseract call boundary so process_next_job succeeds
    deterministically without a real Tesseract binary.
    """
    return (
        mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True),
        mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"),
        mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=_FAKE_RESULT),
    )


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


# --- process_next_job (real execution, Step 1) --------------------------


def test_process_next_job_processes_one_job_end_to_end(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)

    patches = _mock_ocr()
    with patches[0], patches[1], patches[2]:
        processed = process_next_job(db_session, vault)

    assert processed.job_id == job.job_id
    assert processed.status == "completed"
    assert processed.engine == "tesseract-test"
    assert processed.document.ocr_status == "completed"


def test_process_next_job_returns_none_when_queue_empty(db_session: Session, vault: VaultLayout):
    assert process_next_job(db_session, vault) is None


def test_process_next_job_writes_ocr_completed_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document

    patches = _mock_ocr()
    with patches[0], patches[1], patches[2]:
        process_next_job(db_session, vault)

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "ocr_queued", "ocr_completed"]


def test_process_next_job_writes_ocr_text_to_document_pages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document

    patches = _mock_ocr()
    with patches[0], patches[1], patches[2]:
        process_next_job(db_session, vault)

    page = document.pages[0]
    assert page.ocr_text == _FAKE_RESULT.text
    assert page.extraction_confidence == _FAKE_RESULT.confidence
    assert page.extraction_method == "ocr"


def test_process_next_job_without_tesseract_fails_for_real(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """No mocking at all -- this test environment genuinely has no
    Tesseract binary installed, so this exercises the real failure path
    rather than a simulated one.
    """
    job = _queued_job(db_session, vault, sample_case, tmp_path)

    processed = process_next_job(db_session, vault)

    assert processed.status == "failed"
    assert "Tesseract" in processed.error
    assert processed.document.ocr_status == "failed"


def test_process_next_job_never_modifies_the_stored_original(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _queued_job(db_session, vault, sample_case, tmp_path)
    document = job.document
    stored_path = vault.root / document.stored_path
    hash_before = compute_sha256(stored_path)
    content_before = stored_path.read_bytes()

    patches = _mock_ocr()
    with patches[0], patches[1], patches[2]:
        process_next_job(db_session, vault)

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
    patches = _mock_ocr()
    with patches[0], patches[1], patches[2]:
        process_next_job(db_session, vault)  # -> completed

    count = sweep_stuck_jobs(db_session)

    assert count == 0


def test_sweep_stuck_jobs_returns_zero_when_nothing_stuck(db_session: Session):
    assert sweep_stuck_jobs(db_session) == 0
