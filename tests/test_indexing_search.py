"""Tests for app/core/indexing/search.py -- full-text search over
extracted document text.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import fitz
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.indexing.search import _quote_as_phrase, search_case_documents
from app.core.ingestion.service import ingest_document
from app.core.tagging import tag_document
from app.db.models import Case, DocumentDatePrecision
from app.core.vault import VaultLayout


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _ingest_and_extract(db, vault, case, path: Path, filename: str, **kwargs):
    document = ingest_document(db, vault, case, path, filename, actor="test-user", **kwargs)
    db.commit()
    extract_document(db, vault, document, actor="test-user")
    db.commit()
    return document


# --- phrase quoting --------------------------------------------------------


def test_quote_as_phrase_wraps_in_quotes():
    assert _quote_as_phrase("apples") == '"apples"'


def test_quote_as_phrase_escapes_internal_quotes():
    assert _quote_as_phrase('say "hi"') == '"say ""hi"""'


def test_quote_as_phrase_neutralizes_fts5_operators():
    """Without phrase-quoting, 'AND'/'OR'/unbalanced parens are FTS5 query
    syntax and could raise a syntax error on ordinary user input.
    """
    result = _quote_as_phrase("cats AND (dogs OR")
    assert result == '"cats AND (dogs OR"'


# --- basic search behavior --------------------------------------------------


def test_search_finds_matching_text(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "The transportation plan was revised in March.")
    _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")

    results = search_case_documents(db_session, sample_case.case_id, "transportation")

    assert len(results) == 1
    assert results[0].original_filename == "doc.pdf"
    assert results[0].page_number == 1
    assert "transportation" in results[0].snippet.lower()


def test_search_no_match_returns_empty_list(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Nothing relevant here.")
    _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")

    results = search_case_documents(db_session, sample_case.case_id, "nonexistentword")
    assert results == []


def test_empty_query_returns_no_results_without_querying_fts(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "Some content.")
    _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")

    assert search_case_documents(db_session, sample_case.case_id, "") == []
    assert search_case_documents(db_session, sample_case.case_id, "   ") == []


def test_search_is_scoped_to_case(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    pdf_a = tmp_path / "a.pdf"
    _make_pdf(pdf_a, "shared keyword appears here in case A")
    _ingest_and_extract(db_session, vault, sample_case, pdf_a, "a.pdf")

    pdf_b = tmp_path / "b.pdf"
    _make_pdf(pdf_b, "shared keyword appears here in case B")
    _ingest_and_extract(db_session, vault, other_case, pdf_b, "b.pdf")

    results_in_sample_case = search_case_documents(db_session, sample_case.case_id, "shared keyword")
    assert len(results_in_sample_case) == 1
    assert results_in_sample_case[0].original_filename == "a.pdf"


# --- filters -----------------------------------------------------------


def test_search_filters_by_document_type(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    from app.db.models import DocumentType

    iep_type = db_session.query(DocumentType).filter_by(name="IEP").one()
    other_type = db_session.query(DocumentType).filter_by(name="Correspondence").one()

    pdf_iep = tmp_path / "iep.pdf"
    _make_pdf(pdf_iep, "keyword shared across both documents")
    _ingest_and_extract(
        db_session, vault, sample_case, pdf_iep, "iep.pdf", document_type_id=iep_type.type_id
    )

    pdf_letter = tmp_path / "letter.pdf"
    _make_pdf(pdf_letter, "keyword shared across both documents")
    _ingest_and_extract(
        db_session, vault, sample_case, pdf_letter, "letter.pdf",
        document_type_id=other_type.type_id,
    )

    results = search_case_documents(
        db_session, sample_case.case_id, "keyword", document_type_id=iep_type.type_id
    )
    assert len(results) == 1
    assert results[0].original_filename == "iep.pdf"


def test_search_filters_by_needs_ocr(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    # A native-text PDF (needs_ocr False) and a scanned image-only PDF that
    # coincidentally also has extractable text elsewhere won't both match
    # the same query easily -- instead, verify the needs_ocr=False filter
    # excludes an image document entirely (it never matches text search
    # anyway) is implicit; here we confirm the filter narrows a normal
    # text match set correctly using two native documents distinguished by
    # a manually-set needs_ocr flag on one of them isn't directly possible
    # via ingest, so this test uses the natural case: neither doc needs OCR.
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "keyword for ocr filter test")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    assert document.needs_ocr is False

    results_no_ocr = search_case_documents(
        db_session, sample_case.case_id, "keyword", needs_ocr=False
    )
    assert len(results_no_ocr) == 1

    results_needs_ocr = search_case_documents(
        db_session, sample_case.case_id, "keyword", needs_ocr=True
    )
    assert results_needs_ocr == []


def test_search_filters_by_exact_date_range(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "date filter keyword test")
    _ingest_and_extract(
        db_session, vault, sample_case, pdf_path, "doc.pdf",
        document_date=date(2024, 3, 15),
    )

    in_window = search_case_documents(
        db_session, sample_case.case_id, "date filter",
        date_from=date(2024, 3, 1), date_to=date(2024, 3, 31),
    )
    assert len(in_window) == 1

    outside_window = search_case_documents(
        db_session, sample_case.case_id, "date filter",
        date_from=date(2024, 4, 1), date_to=date(2024, 4, 30),
    )
    assert outside_window == []


def test_search_filters_by_overlapping_range_date(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "range overlap keyword test")
    _ingest_and_extract(
        db_session, vault, sample_case, pdf_path, "doc.pdf",
        document_date=date(2024, 3, 1),
        document_date_precision=DocumentDatePrecision.RANGE,
        document_date_range_end=date(2024, 3, 31),
    )

    # Filter window partially overlaps the document's range -> should match.
    overlapping = search_case_documents(
        db_session, sample_case.case_id, "range overlap",
        date_from=date(2024, 3, 20), date_to=date(2024, 4, 10),
    )
    assert len(overlapping) == 1

    # Filter window entirely before the document's range -> should not match.
    non_overlapping = search_case_documents(
        db_session, sample_case.case_id, "range overlap",
        date_from=date(2024, 1, 1), date_to=date(2024, 2, 1),
    )
    assert non_overlapping == []


def test_search_date_filter_with_only_one_bound(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "one bound keyword test")
    _ingest_and_extract(
        db_session, vault, sample_case, pdf_path, "doc.pdf",
        document_date=date(2024, 6, 1),
    )

    only_from_matches = search_case_documents(
        db_session, sample_case.case_id, "one bound", date_from=date(2024, 1, 1)
    )
    assert len(only_from_matches) == 1

    only_from_excludes = search_case_documents(
        db_session, sample_case.case_id, "one bound", date_from=date(2024, 7, 1)
    )
    assert only_from_excludes == []

    only_to_matches = search_case_documents(
        db_session, sample_case.case_id, "one bound", date_to=date(2024, 12, 31)
    )
    assert len(only_to_matches) == 1


def test_search_documents_without_a_date_are_excluded_by_date_filter(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "undated keyword test")
    _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")  # no document_date

    results = search_case_documents(
        db_session, sample_case.case_id, "undated",
        date_from=date(2020, 1, 1), date_to=date(2030, 1, 1),
    )
    assert results == []


# --- tag filter ------------------------------------------------------


def test_search_filters_by_tag(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_tagged = tmp_path / "tagged.pdf"
    _make_pdf(pdf_tagged, "shared keyword for tag filtering")
    tagged_doc = _ingest_and_extract(db_session, vault, sample_case, pdf_tagged, "tagged.pdf")
    tag = tag_document(db_session, tagged_doc, "IEP", actor="test-user")
    db_session.commit()

    pdf_untagged = tmp_path / "untagged.pdf"
    _make_pdf(pdf_untagged, "shared keyword for tag filtering")
    _ingest_and_extract(db_session, vault, sample_case, pdf_untagged, "untagged.pdf")

    results = search_case_documents(
        db_session, sample_case.case_id, "shared keyword", tag_id=tag.tag_id
    )
    assert len(results) == 1
    assert results[0].original_filename == "tagged.pdf"


def test_search_tag_filter_excludes_documents_with_a_different_tag(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "another shared keyword here")
    document = _ingest_and_extract(db_session, vault, sample_case, pdf_path, "doc.pdf")
    tag_document(db_session, document, "IEP", actor="test-user")
    db_session.commit()

    # A tag_id that exists but isn't attached to this document.
    from app.core.tagging import find_or_create_tag

    other_tag = find_or_create_tag(db_session, sample_case, "Correspondence")
    db_session.commit()

    results = search_case_documents(
        db_session, sample_case.case_id, "another shared", tag_id=other_tag.tag_id
    )
    assert results == []


def test_search_tag_filter_combined_with_other_filters(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    from app.db.models import DocumentType

    iep_type = db_session.query(DocumentType).filter_by(name="IEP").one()

    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "combined filters keyword")
    document = _ingest_and_extract(
        db_session, vault, sample_case, pdf_path, "doc.pdf", document_type_id=iep_type.type_id
    )
    tag = tag_document(db_session, document, "urgent", actor="test-user")
    db_session.commit()

    results = search_case_documents(
        db_session,
        sample_case.case_id,
        "combined filters",
        tag_id=tag.tag_id,
        document_type_id=iep_type.type_id,
        needs_ocr=False,
    )
    assert len(results) == 1
