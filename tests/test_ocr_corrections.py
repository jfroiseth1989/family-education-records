"""Tests for app/core/ocr/corrections.py -- create_ocr_correction().

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 3 and
docs/PHASE_3_DECISIONS.md §1/§5/§9.3.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.indexing.search import search_case_documents
from app.core.ingestion.service import ingest_document
from app.core.ocr.corrections import create_ocr_correction
from app.core.ocr.engine import OcrResult
from app.core.ocr.queue import enqueue_ocr_job
from app.core.ocr.service import run_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, OcrCorrection
from app.jobs.worker import claim_next_job


def _ocrd_page(db, vault, case, tmp_path: Path, ocr_text: str = "raw ocr guess", confidence: float = 55.0):
    image_path = tmp_path / "scan.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg for corrections test")
    document = ingest_document(db, vault, case, image_path, "scan.jpg", actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    enqueue_ocr_job(db, document, actor="test-user")
    db.commit()
    job = claim_next_job(db)

    fake = OcrResult(text=ocr_text, confidence=confidence, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake):
        run_ocr_job(db, vault, job)
    db.commit()

    return document, document.pages[0]


# --- validation -----------------------------------------------------------


def test_create_ocr_correction_rejects_page_never_ocrd(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    image_path = tmp_path / "never-ocrd.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0never ocrd")
    document = ingest_document(db_session, vault, sample_case, image_path, "never-ocrd.jpg", actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()
    page = document.pages[0]
    assert page.ocr_text is None

    with pytest.raises(ValueError, match="no OCR text"):
        create_ocr_correction(db_session, page, "some correction", actor="tester")


def test_create_ocr_correction_rejects_empty_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path)

    with pytest.raises(ValueError, match="cannot be empty"):
        create_ocr_correction(db_session, page, "   ", actor="tester")


# --- core behavior ---------------------------------------------------------


def test_create_ocr_correction_creates_a_row(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path)

    correction = create_ocr_correction(db_session, page, "  corrected text  ", actor="tester")
    db_session.commit()

    assert correction.correction_id is not None
    assert correction.corrected_text == "corrected text"
    assert correction.corrected_by == "tester"
    stored = db_session.get(OcrCorrection, correction.correction_id)
    assert stored is not None


def test_create_ocr_correction_never_touches_raw_ocr_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path, ocr_text="original raw text")

    create_ocr_correction(db_session, page, "completely different corrected text", actor="tester")
    db_session.commit()

    assert page.ocr_text == "original raw text"


def test_create_ocr_correction_writes_ocr_corrected_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document, page = _ocrd_page(db_session, vault, sample_case, tmp_path)

    correction = create_ocr_correction(db_session, page, "corrected text", actor="tester")
    db_session.commit()

    # _ocrd_page calls run_ocr_job() directly (not via the worker's
    # process_next_job), which deliberately never writes a custody event
    # itself -- see app/core/ocr/service.py's module docstring -- so no
    # ocr_completed appears here; that path is covered by
    # tests/test_jobs_worker.py instead. This test only needs to confirm
    # ocr_corrected is appended correctly.
    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "ocr_queued", "ocr_corrected"]
    last = document.custody_events[-1]
    assert last.details["correction_id"] == correction.correction_id
    assert last.actor == "tester"


def test_multiple_corrections_are_all_permanently_retained(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path)

    create_ocr_correction(db_session, page, "first correction", actor="c1")
    db_session.commit()
    create_ocr_correction(db_session, page, "second correction", actor="c2")
    db_session.commit()

    all_corrections = db_session.query(OcrCorrection).filter_by(page_id=page.page_id).all()
    assert [c.corrected_text for c in all_corrections] == ["first correction", "second correction"]


# --- search reindex ---------------------------------------------------


def test_correction_makes_corrected_text_searchable(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path, ocr_text="wrongspelling here")

    create_ocr_correction(db_session, page, "correctspelling here", actor="tester")
    db_session.commit()

    results = search_case_documents(db_session, sample_case.case_id, "correctspelling")
    assert len(results) == 1


def test_correction_removes_raw_text_from_search_index(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path, ocr_text="wrongspelling here")

    create_ocr_correction(db_session, page, "correctspelling here", actor="tester")
    db_session.commit()

    results = search_case_documents(db_session, sample_case.case_id, "wrongspelling")
    assert results == []


def test_second_correction_correctly_replaces_first_in_search_index(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """The subtle case: the second correction's reindex must delete the
    *first* correction's indexed value, not the original raw OCR text
    (which is no longer what's actually indexed at that point).
    """
    _, page = _ocrd_page(db_session, vault, sample_case, tmp_path, ocr_text="original raw")

    create_ocr_correction(db_session, page, "firstfix text", actor="c1")
    db_session.commit()
    create_ocr_correction(db_session, page, "secondfix text", actor="c2")
    db_session.commit()

    assert len(search_case_documents(db_session, sample_case.case_id, "secondfix")) == 1
    assert search_case_documents(db_session, sample_case.case_id, "firstfix") == []
    assert search_case_documents(db_session, sample_case.case_id, "original raw") == []


def test_correction_does_not_affect_other_pages_search_results(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _, page_a = _ocrd_page(db_session, vault, sample_case, tmp_path, ocr_text="page a shared_marker text")
    doc_b_path = tmp_path / "b.jpg"
    doc_b_path.write_bytes(b"\xff\xd8\xff\xe0second doc for isolation test")
    from app.core.ingestion.service import ingest_document as _ingest

    doc_b = _ingest(db_session, vault, sample_case, doc_b_path, "b.jpg", actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, doc_b, actor="test-user")
    db_session.commit()
    enqueue_ocr_job(db_session, doc_b, actor="test-user")
    db_session.commit()
    job_b = claim_next_job(db_session)
    fake_b = OcrResult(text="page b shared_marker text", confidence=70.0, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="t"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake_b):
        run_ocr_job(db_session, vault, job_b)
    db_session.commit()

    create_ocr_correction(db_session, page_a, "page a corrected shared_marker text", actor="tester")
    db_session.commit()

    results = search_case_documents(db_session, sample_case.case_id, "shared_marker")
    assert len(results) == 2  # both pages still findable, independently
