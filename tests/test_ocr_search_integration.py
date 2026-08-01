"""Verification that OCR'd text is searchable end-to-end, with zero new
indexing code (docs/PHASE_3_IMPLEMENTATION_PLAN.md §8/Step 2).

`document_text_fts`'s sync triggers (Phase 2 Step 2, `AFTER INSERT/
UPDATE/DELETE ON document_pages`) already cover the `ocr_text` column --
this file proves that empirically, by actually running OCR (mocked at
the pytesseract boundary) and then searching for the resulting text,
rather than just trusting the prediction.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.indexing.search import search_case_documents
from app.core.ingestion.service import ingest_document
from app.core.ocr.engine import OcrResult
from app.core.ocr.queue import enqueue_ocr_job
from app.core.ocr.service import run_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case
from app.jobs.worker import claim_next_job


def _ocr_an_image(db, vault, case, tmp_path: Path, text: str, confidence: float = 80.0):
    image_path = tmp_path / "scan.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg for search integration test")
    document = ingest_document(db, vault, case, image_path, "scan.jpg", actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    enqueue_ocr_job(db, document, actor="test-user")
    db.commit()
    job = claim_next_job(db)

    fake = OcrResult(text=text, confidence=confidence, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake):
        run_ocr_job(db, vault, job)
    db.commit()
    return document


def test_ocrd_text_is_found_by_search_with_no_new_indexing_code(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document = _ocr_an_image(
        db_session, vault, sample_case, tmp_path, "a distinctive ocr keyword appears here"
    )

    results = search_case_documents(db_session, sample_case.case_id, "distinctive ocr keyword")

    assert len(results) == 1
    assert results[0].document_id == document.document_id
    assert results[0].page_number == 1
    assert "distinctive" in results[0].snippet.lower()


def test_search_still_scoped_to_case_for_ocrd_documents(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    _ocr_an_image(db_session, vault, sample_case, tmp_path, "shared ocr keyword in case one")

    results_in_other_case = search_case_documents(db_session, other_case.case_id, "shared ocr keyword")
    assert results_in_other_case == []


def test_search_finds_ocrd_text_alongside_native_text_in_same_case(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """Native and OCR'd documents both live in document_text_fts -- a
    single query can hit either kind, unlike notes search (Step 5, Phase
    2) which is a categorically separate index.
    """
    txt_path = tmp_path / "native.txt"
    txt_path.write_text("a shared unique_marker_term in native text")
    native_doc = ingest_document(db_session, vault, sample_case, txt_path, "native.txt", actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, native_doc, actor="test-user")
    db_session.commit()

    _ocr_an_image(db_session, vault, sample_case, tmp_path, "a shared unique_marker_term in ocr text")

    results = search_case_documents(db_session, sample_case.case_id, "unique_marker_term")
    assert len(results) == 2
    filenames = {r.original_filename for r in results}
    assert filenames == {"native.txt", "scan.jpg"}
