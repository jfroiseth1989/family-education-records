"""Tests for app/core/ocr/queue.py -- enqueueing a document for OCR.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. This module only ever
creates a queued job and logs the custody event -- no OCR execution
exists yet (that's Step 1).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.ingestion.service import ingest_document
from app.core.ocr.queue import enqueue_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, Document, OcrJob


def _ingest_and_extract_image(db, vault, case, tmp_path: Path, filename: str = "scan.jpg") -> Document:
    """An image file is always flagged needs_ocr by Phase 2's extraction."""
    image_path = tmp_path / filename
    # Content varies by filename so two calls in the same case never
    # collide with ingestion's byte-identical-content dedup rule.
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes for " + filename.encode())
    document = ingest_document(db, vault, case, image_path, filename, actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    assert document.needs_ocr is True
    return document


def test_enqueue_ocr_job_creates_queued_job(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document = _ingest_and_extract_image(db_session, vault, sample_case, tmp_path)

    job = enqueue_ocr_job(db_session, document, actor="test-user")
    db_session.commit()

    assert job.job_id is not None
    assert job.status == "queued"
    assert job.document_id == document.document_id

    stored = db_session.get(OcrJob, job.job_id)
    assert stored is not None
    assert stored.status == "queued"


def test_enqueue_ocr_job_sets_document_ocr_status_queued(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document = _ingest_and_extract_image(db_session, vault, sample_case, tmp_path)
    assert document.ocr_status is None

    enqueue_ocr_job(db_session, document, actor="test-user")
    db_session.commit()

    assert document.ocr_status == "queued"


def test_enqueue_ocr_job_writes_ocr_queued_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document = _ingest_and_extract_image(db_session, vault, sample_case, tmp_path)

    job = enqueue_ocr_job(db_session, document, actor="test-user")
    db_session.commit()

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "ocr_queued"]
    last_event = document.custody_events[-1]
    assert last_event.details == {"job_id": job.job_id}


def test_multiple_documents_can_each_have_their_own_queued_job(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    doc_a = _ingest_and_extract_image(db_session, vault, sample_case, tmp_path, "a.jpg")
    doc_b = _ingest_and_extract_image(db_session, vault, sample_case, tmp_path, "b.jpg")

    job_a = enqueue_ocr_job(db_session, doc_a, actor="test-user")
    job_b = enqueue_ocr_job(db_session, doc_b, actor="test-user")
    db_session.commit()

    assert job_a.job_id != job_b.job_id
    all_jobs = db_session.scalars(select(OcrJob)).all()
    assert len(all_jobs) == 2
