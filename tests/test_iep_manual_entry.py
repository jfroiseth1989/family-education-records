"""Tests for app/core/iep_extraction/manual.py (IEP Consistency Review
Step 2 manual/assisted entry path) -- core-module level, no HTTP layer.

See tests/test_api_iep_consistency.py for the route-level (form POST)
equivalents.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.orm import Session

from app.core.iep_extraction.manual import InvalidManualRecordRangeError, create_manual_service_record
from app.db.models import Case, Document, DocumentPage, IepRecord

_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"manual-{_document_counter}.pdf"
    digest = hashlib.sha256(f"{case.case_id}:{filename}:{_document_counter}".encode()).hexdigest()
    document = Document(
        case_id=case.case_id,
        original_filename=filename,
        stored_path=f"cases/{case.case_id}/documents/{filename}",
        sha256_hash=digest,
        file_size_bytes=100,
        ingested_by="test-user",
    )
    db.add(document)
    db.flush()
    return document


def _page(db: Session, document: Document, text: str, page_number: int = 1) -> DocumentPage:
    page = DocumentPage(
        document_id=document.document_id,
        page_number=page_number,
        extracted_text=text,
        extraction_method="native",
        char_count=len(text),
        source_sha256=document.sha256_hash,
    )
    db.add(page)
    db.flush()
    return page


def test_create_manual_record_with_all_fields(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    text = "Occupational therapy provided in the gym."
    page = _page(db_session, document, text)

    record = create_manual_service_record(
        db_session,
        sample_case,
        document,
        page,
        0,
        len("Occupational therapy"),
        service_name="Occupational therapy",
        minutes=30.0,
        frequency_count=2.0,
        frequency_period="week",
        location="the gym",
        provider="Ms. Rivera",
        actor="test-user",
    )
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert stored.document_id == document.document_id
    assert stored.extraction_method == "manual"
    assert stored.comparison_key == "occupational therapy"
    assert stored.section_label == "Page 1"

    values = {f.field_type.name: f for f in stored.fields}
    assert values["service_name"].text_value == "Occupational therapy"
    assert values["minutes"].numeric_value == 30.0
    assert values["frequency_count"].numeric_value == 2.0
    assert values["frequency_period"].text_value == "week"
    assert values["location"].text_value == "the gym"
    assert values["provider"].text_value == "Ms. Rivera"
    for field in stored.fields:
        assert field.citation.quoted_text == "Occupational therapy"
        assert field.citation.page_id == page.page_id


def test_create_manual_record_with_only_service_name(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    text = "Counseling"
    page = _page(db_session, document, text)

    record = create_manual_service_record(
        db_session,
        sample_case,
        document,
        page,
        0,
        len(text),
        service_name="Counseling",
        minutes=None,
        frequency_count=None,
        frequency_period=None,
        location=None,
        provider=None,
        actor="test-user",
    )
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert len(stored.fields) == 1
    assert stored.fields[0].field_type.name == "service_name"


def test_empty_selection_range_is_rejected(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Some text")

    with pytest.raises(InvalidManualRecordRangeError):
        create_manual_service_record(
            db_session,
            sample_case,
            document,
            page,
            5,
            5,
            service_name="OT",
            minutes=None,
            frequency_count=None,
            frequency_period=None,
            location=None,
            provider=None,
            actor="test-user",
        )


def test_out_of_bounds_selection_range_is_rejected(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Short")

    with pytest.raises(InvalidManualRecordRangeError):
        create_manual_service_record(
            db_session,
            sample_case,
            document,
            page,
            0,
            999,
            service_name="OT",
            minutes=None,
            frequency_count=None,
            frequency_period=None,
            location=None,
            provider=None,
            actor="test-user",
        )


def test_empty_service_name_is_rejected(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Some page text")

    with pytest.raises(ValueError, match="Service name cannot be empty"):
        create_manual_service_record(
            db_session,
            sample_case,
            document,
            page,
            0,
            4,
            service_name="   ",
            minutes=None,
            frequency_count=None,
            frequency_period=None,
            location=None,
            provider=None,
            actor="test-user",
        )


def test_quoted_text_comes_from_stored_extraction_not_caller(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    text = "Speech-language therapy details here."
    page = _page(db_session, document, text)

    record = create_manual_service_record(
        db_session,
        sample_case,
        document,
        page,
        0,
        6,
        service_name="Speech",
        minutes=None,
        frequency_count=None,
        frequency_period=None,
        location=None,
        provider=None,
        actor="test-user",
    )
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    citation = stored.fields[0].citation
    assert citation.quoted_text == text[0:6]
    assert citation.quoted_text == "Speech"
