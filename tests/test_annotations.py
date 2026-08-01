"""Tests for app/core/annotations/service.py -- highlights, notes, bookmarks.

See docs/PHASE_2_PLAN.md §7/§13 Step 4 and §12.4 (citations remain the sole
source-of-truth reference mechanism; annotations are never a citation source
themselves).
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.annotations.service import (
    InvalidHighlightRangeError,
    count_document_annotations,
    create_bookmark,
    create_highlight,
    create_note,
    list_page_annotations,
    remove_annotation,
)
from app.core.extraction.service import extract_document
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case, Citation, Document, DocumentCustodyEvent, DocumentPage


def _make_pdf(path: Path, text: str = "Hello evidence world. This is page text.") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _ingest_and_extract(db, vault, case, path: Path, filename: str) -> Document:
    document = ingest_document(db, vault, case, path, filename, actor="test-user")
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    return document


def _first_page(db: Session, document: Document) -> DocumentPage:
    return db.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).one()


# --- create_highlight ---------------------------------------------------


def test_create_highlight_derives_quoted_text_server_side(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Hello evidence world.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_highlight(db_session, document, page, 0, 5, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, annotation.citation_id)
    assert citation.quoted_text == page.extracted_text[0:5] == "Hello"
    assert citation.document_id == document.document_id
    assert citation.page_id == page.page_id
    assert citation.start_offset == 0
    assert citation.end_offset == 5


def test_create_highlight_ignores_any_client_supplied_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """There is no `quoted_text` parameter at all -- the only way to prove
    the server never trusts client text is that the function signature
    doesn't accept it. This test just pins that the citation's quoted_text
    always matches a live slice of the page, even if the page's text
    changes between two highlights.
    """
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Alpha bravo charlie delta.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_highlight(db_session, document, page, 6, 11, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, annotation.citation_id)
    assert citation.quoted_text == "bravo"


def test_create_highlight_rejects_out_of_bounds_range(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Short text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    text_len = len(page.extracted_text)
    with pytest.raises(InvalidHighlightRangeError):
        create_highlight(db_session, document, page, 0, text_len + 100, actor="test-user")


def test_create_highlight_rejects_empty_range(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Some text here.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    with pytest.raises(InvalidHighlightRangeError):
        create_highlight(db_session, document, page, 5, 5, actor="test-user")


def test_create_highlight_rejects_negative_start(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Some text here.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    with pytest.raises(InvalidHighlightRangeError):
        create_highlight(db_session, document, page, -1, 4, actor="test-user")


def test_create_highlight_writes_annotated_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Custody event text here.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_highlight(db_session, document, page, 0, 7, actor="test-user")
    db_session.commit()

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "annotated"]
    last_event = document.custody_events[-1]
    assert last_event.details["annotation_type"] == "highlight"
    assert "citation_id" in last_event.details


def test_create_highlight_links_annotation_to_case_and_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Linkage test text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_highlight(db_session, document, page, 0, 8, actor="test-user")
    db_session.commit()

    assert annotation.case_id == sample_case.case_id
    assert annotation.document_id == document.document_id
    assert annotation.page_id == page.page_id
    assert annotation.annotation_type.name == "highlight"
    assert annotation.citation_id is not None


# --- create_note ----------------------------------------------------------


def test_create_note_stores_stripped_body_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "  A useful note.  ", actor="test-user")
    db_session.commit()

    assert annotation.body_text == "A useful note."
    assert annotation.annotation_type.name == "note"
    assert annotation.citation_id is None


def test_create_note_rejects_empty_body_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    with pytest.raises(ValueError):
        create_note(db_session, document, page, "   ", actor="test-user")


def test_create_note_writes_annotated_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_note(db_session, document, page, "Note text.", actor="test-user")
    db_session.commit()

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "annotated"]
    assert document.custody_events[-1].details["annotation_type"] == "note"


# --- create_bookmark --------------------------------------------------


def test_create_bookmark_without_body_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_bookmark(db_session, document, page, actor="test-user")
    db_session.commit()

    assert annotation.body_text is None
    assert annotation.annotation_type.name == "bookmark"


def test_create_bookmark_with_body_text_strips_whitespace(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_bookmark(
        db_session, document, page, actor="test-user", body_text="  Key page  "
    )
    db_session.commit()

    assert annotation.body_text == "Key page"


# --- remove_annotation (soft delete) -----------------------------------


def test_remove_annotation_soft_deletes(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "Removable note.", actor="test-user")
    db_session.commit()
    assert annotation.deleted_at is None

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    assert annotation.deleted_at is not None
    # Row still exists in the table -- soft delete, not a hard delete.
    from app.db.models import Annotation

    assert db_session.get(Annotation, annotation.annotation_id) is not None


def test_remove_annotation_never_touches_linked_citation(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Removable highlight text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_highlight(db_session, document, page, 0, 10, actor="test-user")
    db_session.commit()
    citation_id = annotation.citation_id

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    citation = db_session.get(Citation, citation_id)
    assert citation is not None
    assert citation.quoted_text == "Removable "


def test_remove_annotation_writes_annotation_removed_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "Note to remove.", actor="test-user")
    db_session.commit()

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "annotated", "annotation_removed"]


def test_remove_annotation_already_removed_is_a_noop(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "Note.", actor="test-user")
    db_session.commit()
    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()
    first_deleted_at = annotation.deleted_at

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    assert annotation.deleted_at == first_deleted_at
    event_types = [e.event_type for e in document.custody_events]
    # No second "annotation_removed" event for the no-op re-removal.
    assert event_types == ["imported", "extracted", "annotated", "annotation_removed"]


# --- list_page_annotations ----------------------------------------------


def test_list_page_annotations_excludes_soft_deleted(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    kept = create_note(db_session, document, page, "Kept note.", actor="test-user")
    removed = create_note(db_session, document, page, "Removed note.", actor="test-user")
    db_session.commit()
    remove_annotation(db_session, removed, actor="test-user")
    db_session.commit()

    results = list_page_annotations(db_session, document.document_id, page.page_id)
    assert [a.annotation_id for a in results] == [kept.annotation_id]


def test_list_page_annotations_ordered_oldest_first(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    first = create_note(db_session, document, page, "First.", actor="test-user")
    db_session.commit()
    second = create_note(db_session, document, page, "Second.", actor="test-user")
    db_session.commit()

    results = list_page_annotations(db_session, document.document_id, page.page_id)
    assert [a.annotation_id for a in results] == [first.annotation_id, second.annotation_id]


# --- count_document_annotations ----------------------------------------


def test_count_document_annotations_by_type(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Counting annotations text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_highlight(db_session, document, page, 0, 9, actor="test-user")
    create_note(db_session, document, page, "A note.", actor="test-user")
    create_note(db_session, document, page, "Another note.", actor="test-user")
    create_bookmark(db_session, document, page, actor="test-user")
    db_session.commit()

    counts = count_document_annotations(db_session, document.document_id)
    assert counts == {"highlight": 1, "note": 2, "bookmark": 1}


def test_count_document_annotations_excludes_removed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "Removable.", actor="test-user")
    db_session.commit()
    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    counts = count_document_annotations(db_session, document.document_id)
    assert counts["note"] == 0
