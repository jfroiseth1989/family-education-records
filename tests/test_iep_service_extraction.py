"""End-to-end tests for app/core/iep_extraction/services.py and
app/core/iep_extraction/records.py (IEP Consistency Review Step 2).

Mirrors tests/test_date_extraction.py's end-to-end section: real DB
rows, `extract_service_records()` exercised directly against a
Document/DocumentPage pair, no HTTP layer involved (see
tests/test_api_iep_consistency.py for the route-level tests).
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.iep_extraction.records import FieldValue, create_record_with_fields
from app.core.iep_extraction.services import METHOD, extract_service_records, normalize_service_name
from app.db.models import Case, Citation, Document, DocumentPage, IepRecord, IepRecordField

_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"iep-{_document_counter}.pdf"
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


def test_normalize_service_name_lowercases_and_collapses_whitespace():
    assert normalize_service_name("  Speech-Language   Therapy ") == "speech-language therapy"


def test_extract_creates_record_with_citation_and_fields(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Speech-language therapy — 30 minutes, 2x/week")

    created = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert len(created) == 1
    record = db_session.get(IepRecord, created[0].record_id)
    assert record.document_id == document.document_id
    assert record.communication_id is None
    assert record.extraction_method == METHOD
    assert record.comparison_key == "speech-language therapy"
    assert record.section_label == "Page 1"

    values = {f.field_type.name: f for f in record.fields}
    assert values["service_name"].text_value == "Speech-language therapy"
    assert values["minutes"].numeric_value == 30.0
    assert values["frequency_count"].numeric_value == 2.0
    assert values["frequency_period"].text_value == "week"

    citation = values["minutes"].citation
    assert citation.page_id == page.page_id
    assert citation.quoted_text == "Speech-language therapy — 30 minutes, 2x/week"
    assert citation.text_source == "native"


def test_extract_finds_nothing_on_a_page_with_no_service_lines(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "This page has no services listed on it.")

    created = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert created == []


def test_extract_scans_every_page(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "Speech-language therapy — 30 minutes, 2x/week", page_number=1)
    _page(db_session, document, "Occupational therapy — 20 minutes, 1x/week", page_number=2)

    created = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert len(created) == 2
    sections = {r.section_label for r in created}
    assert sections == {"Page 1", "Page 2"}


def test_extract_is_idempotent_on_unchanged_text(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "Speech-language therapy — 30 minutes, 2x/week")

    first = extract_service_records(db_session, sample_case, document)
    db_session.commit()
    second = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert len(first) == 1
    assert second == []
    all_records = db_session.scalars(select(IepRecord)).all()
    assert len(all_records) == 1


def test_extract_finds_new_matches_after_page_text_changes(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Speech-language therapy — 30 minutes, 2x/week")

    first = extract_service_records(db_session, sample_case, document)
    db_session.commit()
    assert len(first) == 1

    page.extracted_text = (
        "Speech-language therapy — 30 minutes, 2x/week\n"
        "Occupational therapy — 20 minutes, 1x/week"
    )
    db_session.commit()

    second = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert len(second) == 1
    all_records = db_session.scalars(select(IepRecord)).all()
    assert len(all_records) == 2


def test_extract_ignores_pages_with_no_effective_text(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = DocumentPage(
        document_id=document.document_id,
        page_number=1,
        extraction_method="none",
        char_count=0,
        needs_ocr=True,
        source_sha256=document.sha256_hash,
    )
    db_session.add(page)
    db_session.commit()

    created = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    assert created == []


def test_extract_captures_location_and_provider_when_present(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "Counseling — 45 minutes, 1x/week in the resource room")

    created = extract_service_records(db_session, sample_case, document)
    db_session.commit()

    values = {f.field_type.name: f for f in created[0].fields}
    assert values["location"].text_value == "the resource room"
    assert "provider" not in values


# --- create_record_with_fields() validation (records.py) -------------------


def _citation(db: Session, document: Document) -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text="30 minutes, 2x/week")
    db.add(citation)
    db.flush()
    return citation


def test_create_record_rejects_document_from_a_different_case(db_session: Session, sample_case: Case):
    from app.db.models import Case as CaseModel

    other_case = CaseModel(label="Other Student")
    db_session.add(other_case)
    db_session.flush()

    document = _document(db_session, other_case)
    with pytest.raises(ValueError, match="different student"):
        create_record_with_fields(
            db_session,
            sample_case,
            document=document,
            record_type_name="service",
            section_label=None,
            comparison_key=None,
            extraction_method="manual",
            actor="test-user",
            field_values=[],
        )


def test_create_record_rejects_unknown_record_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    with pytest.raises(ValueError, match="Unknown IEP record type"):
        create_record_with_fields(
            db_session,
            sample_case,
            document=document,
            record_type_name="not-a-real-record-type",
            section_label=None,
            comparison_key=None,
            extraction_method="manual",
            actor="test-user",
            field_values=[],
        )


def test_create_record_rejects_field_value_kind_mismatch(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    with pytest.raises(ValueError, match="expects a number value"):
        create_record_with_fields(
            db_session,
            sample_case,
            document=document,
            record_type_name="service",
            section_label=None,
            comparison_key=None,
            extraction_method="manual",
            actor="test-user",
            field_values=[FieldValue("minutes", text_value="thirty")],
        )


def test_create_record_rejects_field_with_no_value_set(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    with pytest.raises(ValueError, match="exactly one"):
        create_record_with_fields(
            db_session,
            sample_case,
            document=document,
            record_type_name="service",
            section_label=None,
            comparison_key=None,
            extraction_method="manual",
            actor="test-user",
            field_values=[FieldValue("service_name")],
        )


def test_create_record_rejects_citation_from_a_different_document(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    other_document = _document(db_session, sample_case)
    citation = _citation(db_session, other_document)

    with pytest.raises(ValueError, match="different document"):
        create_record_with_fields(
            db_session,
            sample_case,
            document=document,
            record_type_name="service",
            section_label=None,
            comparison_key=None,
            extraction_method="manual",
            actor="test-user",
            field_values=[FieldValue("service_name", text_value="OT", citation=citation)],
        )


def test_create_record_persists_valid_fields(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    record = create_record_with_fields(
        db_session,
        sample_case,
        document=document,
        record_type_name="service",
        section_label="Page 3",
        comparison_key="ot",
        extraction_method="manual",
        actor="test-user",
        field_values=[
            FieldValue("service_name", text_value="OT", citation=citation),
            FieldValue("minutes", numeric_value=30.0, citation=citation),
        ],
    )
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert len(stored.fields) == 2
    field_by_type = {f.field_type.name: f for f in stored.fields}
    assert field_by_type["service_name"].text_value == "OT"
    assert field_by_type["minutes"].numeric_value == 30.0
