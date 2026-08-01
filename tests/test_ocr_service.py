"""Tests for app/core/ocr/service.py -- run_ocr_job()'s content-level
correctness: hash/file checks, per-page partial failure, archive-before-
overwrite, and PDF page rendering vs. direct image OCR.

Mocks at the pytesseract call boundary (app.core.ocr.service.run_ocr_on_image)
for deterministic, Tesseract-binary-free tests -- see tests/test_ocr_engine.py
for engine.py's own tests and tests/test_jobs_worker.py for one real,
unmocked "Tesseract genuinely missing" test.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import fitz
import pytest
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.files import compute_sha256
from app.core.ingestion.service import ingest_document
from app.core.ocr.engine import OcrResult, WordBox
from app.core.ocr.queue import enqueue_ocr_job
from app.core.ocr.service import run_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, Document, OcrTextHistory
from app.jobs.worker import claim_next_job


def _make_scanned_pdf(path: Path, pages: int = 1) -> None:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 20, 20))
        pixmap.set_rect(pixmap.irect, (200, 0, 0))
        page.insert_image(fitz.Rect(72, 72, 200, 200), pixmap=pixmap)
    doc.save(str(path))
    doc.close()


def _make_mixed_pdf(path: Path) -> None:
    """Page 1: real native text. Page 2: scanned (image, no text)."""
    doc = fitz.open()
    native_page = doc.new_page()
    native_page.insert_text((72, 72), "This page has plenty of real native text on it.")
    scanned_page = doc.new_page()
    pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 20, 20))
    pixmap.set_rect(pixmap.irect, (0, 200, 0))
    scanned_page.insert_image(fitz.Rect(72, 72, 200, 200), pixmap=pixmap)
    doc.save(str(path))
    doc.close()


def _claimed_image_job(db, vault, case, tmp_path: Path, filename: str = "scan.jpg"):
    image_path = tmp_path / filename
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg for " + filename.encode())
    document = ingest_document(db, vault, case, image_path, filename, actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    enqueue_ocr_job(db, document, actor="test-user")
    db.commit()
    return claim_next_job(db)


def _claimed_pdf_job(db, vault, case, pdf_path: Path, filename: str):
    document = ingest_document(db, vault, case, pdf_path, filename, actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    enqueue_ocr_job(db, document, actor="test-user")
    db.commit()
    return claim_next_job(db)


def _mock_engine(**patches):
    ctxs = [
        mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True),
        mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"),
    ]
    if "run_ocr_on_image" in patches:
        ctxs.append(mock.patch("app.core.ocr.service.run_ocr_on_image", patches["run_ocr_on_image"]))
    return ctxs


def _enter_all(ctxs):
    for ctx in ctxs:
        ctx.__enter__()


def _exit_all(ctxs):
    for ctx in reversed(ctxs):
        ctx.__exit__(None, None, None)


# --- failure modes before any page is processed --------------------------


def test_run_ocr_job_fails_when_stored_file_missing(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    stored_path = vault.root / job.document.stored_path
    stored_path.unlink()

    result = run_ocr_job(db_session, vault, job)

    assert result.status == "failed"
    assert "missing" in result.error.lower()


def test_run_ocr_job_fails_when_hash_mismatch(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    stored_path = vault.root / job.document.stored_path
    # Tamper with the stored file out-of-band -- its bytes no longer
    # match the recorded hash.
    original_mode = stored_path.stat().st_mode
    stored_path.chmod(0o600)
    stored_path.write_bytes(b"tampered content that no longer matches the recorded hash")
    stored_path.chmod(original_mode)

    result = run_ocr_job(db_session, vault, job)

    assert result.status == "failed"
    assert "hash" in result.error.lower()


def test_run_ocr_job_fails_for_real_without_tesseract(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """No mocking -- this environment has no real Tesseract binary."""
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)

    result = run_ocr_job(db_session, vault, job)

    assert result.status == "failed"
    assert "Tesseract" in result.error


# --- successful OCR of a standalone image --------------------------------


def test_run_ocr_job_writes_text_confidence_and_word_boxes(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    fake = OcrResult(
        text="Recognized text.", confidence=91.5,
        word_boxes=[WordBox(text="Recognized", left=1, top=2, width=3, height=4, confidence=91.5)],
    )
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        result = run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert result.status == "completed"
    assert result.pages_ocred == [1]
    page = job.document.pages[0]
    assert page.ocr_text == "Recognized text."
    assert page.extraction_confidence == 91.5
    assert page.ocr_word_boxes == [
        {"text": "Recognized", "left": 1, "top": 2, "width": 3, "height": 4, "confidence": 91.5}
    ]


def test_run_ocr_job_sets_extraction_method_to_ocr(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    page = job.document.pages[0]
    assert page.extraction_method == "none"

    fake = OcrResult(text="x", confidence=80.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert page.extraction_method == "ocr"


def test_run_ocr_job_never_touches_extracted_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    page = job.document.pages[0]
    assert page.extracted_text is None

    fake = OcrResult(text="x", confidence=80.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert page.extracted_text is None  # native column untouched by OCR


def test_run_ocr_job_never_modifies_the_stored_original(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    document = job.document
    stored_path = vault.root / document.stored_path
    hash_before = compute_sha256(stored_path)
    content_before = stored_path.read_bytes()

    fake = OcrResult(text="x", confidence=80.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert compute_sha256(stored_path) == hash_before
    assert stored_path.read_bytes() == content_before


# --- PDF page rendering vs. mixed native/scanned pages --------------------


def test_run_ocr_job_renders_pdf_pages_via_pymupdf(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(pdf_path, pages=1)
    job = _claimed_pdf_job(db_session, vault, sample_case, pdf_path, "scanned.pdf")

    fake = OcrResult(text="pdf page text", confidence=70.0, word_boxes=[])
    seen_paths = []

    def _fake_ocr(path):
        seen_paths.append(Path(path))
        return fake

    ctxs = _mock_engine(run_ocr_on_image=_fake_ocr)
    _enter_all(ctxs)
    try:
        result = run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert result.status == "completed"
    assert len(seen_paths) == 1
    # The rendered page is a real temp PNG, not the original PDF path.
    assert seen_paths[0].suffix == ".png"
    assert seen_paths[0] != vault.root / job.document.stored_path


def test_run_ocr_job_only_processes_needs_ocr_pages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "mixed.pdf"
    _make_mixed_pdf(pdf_path)
    job = _claimed_pdf_job(db_session, vault, sample_case, pdf_path, "mixed.pdf")
    document = job.document
    native_page = next(p for p in document.pages if p.page_number == 1)
    scanned_page = next(p for p in document.pages if p.page_number == 2)
    assert native_page.needs_ocr is False
    assert scanned_page.needs_ocr is True
    native_text_before = native_page.extracted_text

    fake = OcrResult(text="scanned page ocr text", confidence=60.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        result = run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert result.pages_ocred == [2]
    assert native_page.extracted_text == native_text_before
    assert native_page.ocr_text is None
    assert scanned_page.ocr_text == "scanned page ocr text"


# --- per-page partial failure --------------------------------------------


def test_run_ocr_job_partial_failure_keeps_successful_pages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "two-scanned.pdf"
    _make_scanned_pdf(pdf_path, pages=2)
    job = _claimed_pdf_job(db_session, vault, sample_case, pdf_path, "two-scanned.pdf")

    fake = OcrResult(text="page one succeeded", confidence=75.0, word_boxes=[])

    def _fake_ocr(path):
        # Fail specifically on the second page's rendered temp file name.
        if "page-2" in str(path):
            raise RuntimeError("simulated OCR failure on page 2")
        return fake

    ctxs = _mock_engine(run_ocr_on_image=_fake_ocr)
    _enter_all(ctxs)
    try:
        result = run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert result.status == "completed_with_errors"
    assert result.pages_ocred == [1]
    assert result.pages_failed == [2]
    assert "page 2" in result.error

    pages = {p.page_number: p for p in job.document.pages}
    assert pages[1].ocr_text == "page one succeeded"
    assert pages[2].ocr_text is None


def test_run_ocr_job_total_failure_when_every_page_fails(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "two-scanned-fail.pdf"
    _make_scanned_pdf(pdf_path, pages=2)
    job = _claimed_pdf_job(db_session, vault, sample_case, pdf_path, "two-scanned-fail.pdf")

    def _always_fail(path):
        raise RuntimeError("simulated total OCR failure")

    ctxs = _mock_engine(run_ocr_on_image=_always_fail)
    _enter_all(ctxs)
    try:
        result = run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    assert result.status == "failed"
    assert result.pages_ocred == []
    assert set(result.pages_failed) == {1, 2}


# --- archive-before-overwrite (ocr_text_history) --------------------------


def test_first_ocr_run_creates_no_history_row(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    fake = OcrResult(text="first run text", confidence=80.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: fake)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)

    history = db_session.query(OcrTextHistory).all()
    assert history == []


def test_reprocess_archives_the_previous_raw_text_before_overwriting(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    job = _claimed_image_job(db_session, vault, sample_case, tmp_path)
    page_id = job.document.pages[0].page_id

    first_result = OcrResult(text="original OCR guess", confidence=55.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: first_result)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, job)
    finally:
        _exit_all(ctxs)
    db_session.commit()

    # Simulate a reprocess: claim a fresh job for the same document and
    # run OCR again with different (improved) output.
    from app.core.ocr.queue import enqueue_ocr_job

    enqueue_ocr_job(db_session, job.document, actor="test-user")
    db_session.commit()
    second_job = claim_next_job(db_session)

    second_result = OcrResult(text="corrected OCR guess on reprocess", confidence=93.0, word_boxes=[])
    ctxs = _mock_engine(run_ocr_on_image=lambda path: second_result)
    _enter_all(ctxs)
    try:
        run_ocr_job(db_session, vault, second_job)
    finally:
        _exit_all(ctxs)
    db_session.commit()  # this session has autoflush=False -- see conftest.py

    # Current raw OCR text reflects the second (latest) run only.
    page = db_session.get(type(job.document.pages[0]), page_id)
    assert page.ocr_text == "corrected OCR guess on reprocess"
    assert page.extraction_confidence == 93.0

    # The first run's text was archived, not simply discarded.
    history = db_session.query(OcrTextHistory).filter_by(page_id=page_id).all()
    assert len(history) == 1
    assert history[0].ocr_text == "original OCR guess"
    assert history[0].extraction_confidence == 55.0
    assert history[0].superseded_by_job_id == second_job.job_id
