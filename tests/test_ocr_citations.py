"""Tests for the Phase 3 Step 1 extension to create_highlight() -- citing
an OCR'd page, and the citations.text_source/source_confidence columns
it sets.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md §7 and docs/PHASE_3_DECISIONS.md
§9.2. The pre-existing native-highlight path (Phase 2 Step 4) is
unmodified in behavior -- one regression test here confirms it still
gets text_source='native'/source_confidence=None, without touching
tests/test_annotations.py itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import fitz
import pytest
from sqlalchemy.orm import Session

from app.core.annotations.service import InvalidHighlightRangeError, create_highlight
from app.core.extraction.service import extract_document
from app.core.ingestion.service import ingest_document
from app.core.ocr.corrections import create_ocr_correction
from app.core.ocr.engine import OcrResult
from app.core.ocr.queue import enqueue_ocr_job
from app.core.vault import VaultLayout
from app.db.models import Case, Citation
from app.jobs.worker import claim_next_job
from app.core.ocr.service import run_ocr_job


def _ocr_an_image_document(db, vault, case, tmp_path: Path, ocr_text: str, confidence: float):
    image_path = tmp_path / "scan.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg for citation test")
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


def test_highlight_on_ocrd_page_derives_quoted_text_from_ocr_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "Recognized evidence text.", 82.0
    )

    annotation = create_highlight(db_session, document, page, 0, 10, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, annotation.citation_id)
    assert citation.quoted_text == "Recognized"


def test_highlight_on_ocrd_page_sets_text_source_ocr_raw(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "Some OCR text here.", 65.5
    )

    annotation = create_highlight(db_session, document, page, 0, 4, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, annotation.citation_id)
    assert citation.text_source == "ocr_raw"
    assert citation.source_confidence == 65.5


def test_highlight_range_validated_against_ocr_text_length(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "Short.", 50.0
    )

    with pytest.raises(InvalidHighlightRangeError):
        create_highlight(db_session, document, page, 0, 999, actor="test-user")


def test_highlight_on_unocrd_page_still_raises_invalid_range(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """A page flagged needs_ocr but never actually OCR'd yet has no
    effective text at all -- any range is invalid, same as before Phase 3.
    """
    image_path = tmp_path / "not-ocrd-yet.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0never ocrd")
    document = ingest_document(db_session, vault, sample_case, image_path, "not-ocrd-yet.jpg", actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()
    page = document.pages[0]

    with pytest.raises(InvalidHighlightRangeError):
        create_highlight(db_session, document, page, 0, 5, actor="test-user")


def test_native_highlight_still_gets_text_source_native(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """Regression check: the pre-existing native-text highlight path
    (Phase 2 Step 4) is unmodified in behavior by this extension.
    """
    pdf_path = tmp_path / "native.pdf"
    doc = fitz.open()
    page_obj = doc.new_page()
    page_obj.insert_text((72, 72), "Native extracted text for regression check.")
    doc.save(str(pdf_path))
    doc.close()

    document = ingest_document(db_session, vault, sample_case, pdf_path, "native.pdf", actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()
    page = document.pages[0]

    annotation = create_highlight(db_session, document, page, 0, 6, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, annotation.citation_id)
    assert citation.quoted_text == "Native"
    assert citation.text_source == "native"
    assert citation.source_confidence is None


# --- immutability regression (Phase 3 Step 3, docs/PHASE_3_DECISIONS.md §9.2) ---


def test_correction_never_retroactively_changes_an_existing_citation(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """The single most important regression test in this file: a citation
    describes what was quoted *at the time it was made*, permanently --
    a correction made afterward must never change it.
    """
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "orignal msspelled text here", 55.0
    )

    pre_correction = create_highlight(db_session, document, page, 0, 8, actor="test-user")
    db_session.commit()
    pre_citation_id = pre_correction.citation_id
    pre_citation = db_session.get(Citation, pre_citation_id)
    assert pre_citation.quoted_text == "orignal "
    assert pre_citation.text_source == "ocr_raw"
    assert pre_citation.source_confidence == 55.0

    create_ocr_correction(db_session, page, "original misspelled text here", actor="corrector")
    db_session.commit()

    # Re-fetch fresh from the DB -- not just checking the same in-memory
    # object was never mutated, but that nothing was ever persisted either.
    db_session.expire(pre_citation)
    reloaded = db_session.get(Citation, pre_citation_id)
    assert reloaded.quoted_text == "orignal "
    assert reloaded.text_source == "ocr_raw"
    assert reloaded.source_confidence == 55.0


def test_highlight_created_after_correction_cites_the_corrected_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "orignal msspelled text here", 55.0
    )

    create_ocr_correction(db_session, page, "original misspelled text here", actor="corrector")
    db_session.commit()

    post_correction = create_highlight(db_session, document, page, 0, 8, actor="test-user")
    db_session.commit()

    post_citation = db_session.get(Citation, post_correction.citation_id)
    assert post_citation.quoted_text == "original"
    assert post_citation.text_source == "ocr_corrected"
    # Confidence still reflects the underlying OCR run, not a new concept.
    assert post_citation.source_confidence == 55.0


def test_two_citations_on_same_page_can_have_different_text_sources(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """Not an inconsistency -- each citation accurately reflects what was
    quoted at its own moment in time.
    """
    document, page = _ocr_an_image_document(
        db_session, vault, sample_case, tmp_path, "orignal msspelled text here", 55.0
    )

    before = create_highlight(db_session, document, page, 0, 8, actor="test-user")
    db_session.commit()

    create_ocr_correction(db_session, page, "original misspelled text here", actor="corrector")
    db_session.commit()

    after = create_highlight(db_session, document, page, 0, 8, actor="test-user")
    db_session.commit()

    before_citation = db_session.get(Citation, before.citation_id)
    after_citation = db_session.get(Citation, after.citation_id)
    assert before_citation.text_source == "ocr_raw"
    assert after_citation.text_source == "ocr_corrected"
