"""Tests for app/core/indexing/notes_search.py -- search over annotation
body text (notes/bookmarks), and its explicit (non-trigger) sync with
app/core/annotations/service.py.
"""

from __future__ import annotations

from pathlib import Path

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.annotations.service import (
    create_bookmark,
    create_highlight,
    create_note,
    remove_annotation,
)
from app.core.extraction.service import extract_document
from app.core.indexing.notes_search import search_case_annotation_notes
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentPage


def _make_pdf(path: Path, text_content: str = "Some page text.") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text_content)
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


# --- indexing on create --------------------------------------------------


def test_create_note_makes_it_searchable(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_note(db_session, document, page, "Follow up about the evaluation.", actor="test-user")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "follow up")
    assert len(results) == 1
    assert results[0].annotation_type == "note"
    assert "follow up" in results[0].snippet.lower()


def test_create_bookmark_with_body_text_is_searchable(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_bookmark(db_session, document, page, actor="test-user", body_text="Key deadline")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "deadline")
    assert len(results) == 1
    assert results[0].annotation_type == "bookmark"


def test_bookmark_without_body_text_is_not_indexed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """A blank bookmark has no `body_text`, so `index_annotation_note`
    must never attempt an FTS insert for it.

    Checked by spying on `db_session.execute` rather than querying
    `annotation_notes_fts` directly: for an *external-content* FTS5 table,
    a plain unfiltered `SELECT ... FROM annotation_notes_fts` (no `MATCH`
    clause) transparently scans the underlying `annotations` content table
    itself, not the FTS index -- so it would show this row regardless of
    whether it was ever actually indexed, making it useless as a check
    here. Only a `MATCH` query (as `search_case_annotation_notes` uses)
    reflects genuine index state.
    """
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    executed_sql: list[str] = []
    original_execute = db_session.execute

    def _tracking_execute(statement, *args, **kwargs):
        executed_sql.append(str(statement))
        return original_execute(statement, *args, **kwargs)

    db_session.execute = _tracking_execute
    try:
        create_bookmark(db_session, document, page, actor="test-user")
    finally:
        db_session.execute = original_execute

    assert not any("annotation_notes_fts" in sql for sql in executed_sql)


def test_highlight_is_never_indexed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """A highlight has no body_text -- it must never appear in notes
    search, since its text belongs to the source document, not the user's
    own notes (see docs/PHASE_2_PLAN.md §6/§12.4).
    """
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Highlightable evidence text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    create_highlight(db_session, document, page, 0, 12, actor="test-user")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "Highlightable")
    assert results == []


# --- deindexing on removal ------------------------------------------------


def test_removed_note_is_no_longer_searchable(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_note(db_session, document, page, "Removable searchable note.", actor="test-user")
    db_session.commit()
    assert len(search_case_annotation_notes(db_session, sample_case.case_id, "Removable")) == 1

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "Removable")
    assert results == []


def test_removing_a_highlight_is_a_harmless_noop_for_the_notes_index(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Removable highlight text.")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    annotation = create_highlight(db_session, document, page, 0, 10, actor="test-user")
    db_session.commit()

    remove_annotation(db_session, annotation, actor="test-user")
    db_session.commit()  # must not raise -- deindex is a no-op for body_text-less annotations


def test_removing_one_note_does_not_affect_another(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    page = _first_page(db_session, document)

    kept = create_note(db_session, document, page, "Kept note about transportation.", actor="test-user")
    removed = create_note(db_session, document, page, "Removed note about transportation.", actor="test-user")
    db_session.commit()

    remove_annotation(db_session, removed, actor="test-user")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "transportation")
    assert len(results) == 1
    assert results[0].annotation_id == kept.annotation_id


# --- search scoping and query behavior ------------------------------------


def test_notes_search_is_scoped_to_case(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    pdf_a = tmp_path / "a.pdf"
    _make_pdf(pdf_a)
    doc_a = _ingest_and_extract(db_session, vault, sample_case, pdf_a, "a.pdf")
    create_note(db_session, doc_a, _first_page(db_session, doc_a), "shared keyword note A", actor="test-user")

    pdf_b = tmp_path / "b.pdf"
    _make_pdf(pdf_b)
    doc_b = _ingest_and_extract(db_session, vault, other_case, pdf_b, "b.pdf")
    create_note(db_session, doc_b, _first_page(db_session, doc_b), "shared keyword note B", actor="test-user")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "shared keyword")
    assert len(results) == 1
    assert results[0].document_id == doc_a.document_id


def test_empty_query_returns_no_results(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    create_note(db_session, document, _first_page(db_session, document), "Some note text.", actor="test-user")
    db_session.commit()

    assert search_case_annotation_notes(db_session, sample_case.case_id, "") == []
    assert search_case_annotation_notes(db_session, sample_case.case_id, "   ") == []


def test_notes_search_never_returns_document_text_hits(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """§12.4: document text and annotation notes are never conflated."""
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "This unique_source_marker appears only in the source document.")
    _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    db_session.commit()

    results = search_case_annotation_notes(db_session, sample_case.case_id, "unique_source_marker")
    assert results == []
