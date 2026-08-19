"""Tests for app/core/iep_consistency/rules.py (IEP Consistency Review
Step 3) -- pure, deterministic within-document service comparison.

Uses real `iep_records`/`iep_record_fields` rows built via
app/core/iep_extraction/records.py::create_record_with_fields() (the
same choke point the real extractors and manual entry go through)
rather than hand-built ORM objects, so these tests exercise the exact
shape `rules.py` sees in production.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.core.iep_extraction.records import FieldValue, create_record_with_fields
from app.core.iep_consistency.rules import compare_service_records_within_document
from app.db.models import Case, Citation, Document

_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"rules-{_document_counter}.pdf"
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


def _citation(db: Session, document: Document, quoted_text: str) -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text=quoted_text)
    db.add(citation)
    db.flush()
    return citation


def _service_record(
    db: Session,
    case: Case,
    document: Document,
    *,
    section_label: str,
    service_name: str = "Speech-language therapy",
    minutes: float | None = 30.0,
    frequency_count: float | None = 2.0,
    frequency_period: str | None = "week",
    location: str | None = None,
    provider: str | None = None,
    quoted_text: str | None = None,
):
    citation = _citation(db, document, quoted_text or f"{service_name} — {section_label}")
    field_values = [FieldValue("service_name", text_value=service_name, citation=citation)]
    if minutes is not None:
        field_values.append(FieldValue("minutes", numeric_value=minutes, citation=citation))
    if frequency_count is not None:
        field_values.append(FieldValue("frequency_count", numeric_value=frequency_count, citation=citation))
    if frequency_period is not None:
        field_values.append(FieldValue("frequency_period", text_value=frequency_period, citation=citation))
    if location is not None:
        field_values.append(FieldValue("location", text_value=location, citation=citation))
    if provider is not None:
        field_values.append(FieldValue("provider", text_value=provider, citation=citation))

    record = create_record_with_fields(
        db,
        case,
        document=document,
        record_type_name="service",
        section_label=section_label,
        comparison_key=service_name.lower(),
        extraction_method="manual",
        actor="test-user",
        field_values=field_values,
    )
    db.flush()
    return record


def test_identical_services_produce_no_flag(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="Services section")
    b = _service_record(db_session, sample_case, document, section_label="Summary section")
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert candidates == []


def test_minutes_mismatch_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="Services section", minutes=30.0)
    b = _service_record(db_session, sample_case, document, section_label="Summary section", minutes=20.0)
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.inconsistency_type_name == "service_minutes_mismatch"
    assert candidate.rule_id == "service_schedule_mismatch_v1"
    assert candidate.comparison_mode == "within_document"
    assert candidate.source_a_record_id == a.record_id
    assert candidate.source_b_record_id == b.record_id
    assert candidate.extracted_value_a["values"]["minutes"] == 30.0
    assert candidate.extracted_value_b["values"]["minutes"] == 20.0
    assert candidate.extracted_value_a["section_label"] == "Services section"
    assert candidate.extracted_value_b["section_label"] == "Summary section"
    assert "minutes" in candidate.reason_text.lower()


def test_frequency_count_mismatch_flags_as_frequency_not_minutes(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", frequency_count=2.0)
    b = _service_record(db_session, sample_case, document, section_label="B", frequency_count=1.0)
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert len(candidates) == 1
    assert candidates[0].inconsistency_type_name == "service_frequency_mismatch"
    assert candidates[0].rule_id == "service_schedule_mismatch_v1"
    # Frequency spans two field rows (count + period) -- treated as one
    # record-level dimension, not anchored to a single field.
    assert candidates[0].source_a_field_id is None
    assert candidates[0].source_b_field_id is None


def test_frequency_period_mismatch_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", frequency_period="week")
    b = _service_record(db_session, sample_case, document, section_label="B", frequency_period="month")
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert len(candidates) == 1
    assert candidates[0].inconsistency_type_name == "service_frequency_mismatch"


def test_frequency_period_normalized_before_comparison(db_session: Session, sample_case: Case):
    """"Week" vs "week " (whitespace) is not a real difference."""
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", frequency_period="Week")
    b = _service_record(db_session, sample_case, document, section_label="B", frequency_period="week")
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert candidates == []


def test_location_mismatch_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", location="the gym")
    b = _service_record(db_session, sample_case, document, section_label="B", location="the resource room")
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert len(candidates) == 1
    assert candidates[0].inconsistency_type_name == "service_location_mismatch"
    assert candidates[0].rule_id == "service_location_mismatch_v1"
    assert candidates[0].source_a_field_id is not None


def test_provider_mismatch_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", provider="Ms. Rivera")
    b = _service_record(db_session, sample_case, document, section_label="B", provider="Mr. Chen")
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert len(candidates) == 1
    assert candidates[0].inconsistency_type_name == "service_provider_mismatch"
    assert candidates[0].rule_id == "service_provider_mismatch_v1"


def test_missing_value_on_one_side_is_never_flagged(db_session: Session, sample_case: Case):
    """A field present on only one side is absence, not a mismatch --
    never guessed at (docs/IEP_CONSISTENCY_REVIEW_PLAN.md §4)."""
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", location="the gym")
    b = _service_record(db_session, sample_case, document, section_label="B", location=None)
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert candidates == []


def test_different_comparison_keys_are_never_compared(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", service_name="Speech-language therapy", minutes=30.0)
    b = _service_record(db_session, sample_case, document, section_label="B", service_name="Occupational therapy", minutes=20.0)
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    assert candidates == []


def test_source_a_is_always_the_lower_record_id(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", minutes=30.0)
    b = _service_record(db_session, sample_case, document, section_label="B", minutes=20.0)
    db_session.commit()
    assert a.record_id < b.record_id

    candidates = compare_service_records_within_document([a, b])
    assert candidates[0].source_a_record_id == a.record_id
    assert candidates[0].source_b_record_id == b.record_id


def test_three_records_same_key_produces_all_pairwise_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(db_session, sample_case, document, section_label="A", minutes=30.0)
    b = _service_record(db_session, sample_case, document, section_label="B", minutes=20.0)
    c = _service_record(db_session, sample_case, document, section_label="C", minutes=10.0)
    db_session.commit()

    candidates = compare_service_records_within_document([a, b, c])
    minutes_flags = [c for c in candidates if c.inconsistency_type_name == "service_minutes_mismatch"]
    assert len(minutes_flags) == 3
    pairs = {(c.source_a_record_id, c.source_b_record_id) for c in minutes_flags}
    assert pairs == {(a.record_id, b.record_id), (a.record_id, c.record_id), (b.record_id, c.record_id)}


def test_snapshot_contains_quoted_text_and_document_identity(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a = _service_record(
        db_session,
        sample_case,
        document,
        section_label="Services section",
        minutes=30.0,
        quoted_text="Speech-language therapy — 30 minutes, 2x/week",
    )
    b = _service_record(
        db_session,
        sample_case,
        document,
        section_label="Summary section",
        minutes=20.0,
        quoted_text="Speech-language therapy — 20 minutes, 1x/week",
    )
    db_session.commit()

    candidates = compare_service_records_within_document([a, b])
    snap_a = candidates[0].extracted_value_a
    assert snap_a["record_id"] == a.record_id
    assert snap_a["document_id"] == document.document_id
    assert snap_a["extraction_method"] == "manual"
    assert snap_a["quoted_text"] == "Speech-language therapy — 30 minutes, 2x/week"


def test_records_without_comparison_key_are_skipped(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document, "some service line")
    record = create_record_with_fields(
        db_session,
        sample_case,
        document=document,
        record_type_name="service",
        section_label="A",
        comparison_key=None,
        extraction_method="manual",
        actor="test-user",
        field_values=[
            FieldValue("service_name", text_value="Mystery service", citation=citation),
            FieldValue("minutes", numeric_value=30.0, citation=citation),
        ],
    )
    db_session.commit()

    candidates = compare_service_records_within_document([record])
    assert candidates == []
