"""Tests for app/core/iep_consistency/service.py (IEP Consistency
Review Step 3) -- idempotent flag creation and the
pending/confirmed/dismissed review lifecycle.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.iep_consistency.service import (
    confirm_flag,
    dismiss_flag,
    list_inconsistency_flags,
    scan_document_for_service_inconsistencies,
    set_flag_note,
)
from app.core.iep_extraction.records import FieldValue, create_record_with_fields
from app.db.models import Case, Citation, Document, IepInconsistencyFlag, IepRecord, IepRecordField

_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"svc-{_document_counter}.pdf"
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


def _service_record(db: Session, case: Case, document: Document, *, section_label: str, minutes: float):
    citation = _citation(db, document, f"Speech-language therapy — {minutes} minutes")
    record = create_record_with_fields(
        db,
        case,
        document=document,
        record_type_name="service",
        section_label=section_label,
        comparison_key="speech-language therapy",
        extraction_method="manual",
        actor="test-user",
        field_values=[
            FieldValue("service_name", text_value="Speech-language therapy", citation=citation),
            FieldValue("minutes", numeric_value=minutes, citation=citation),
        ],
    )
    db.flush()
    return record


def _two_conflicting_records(db: Session, case: Case, document: Document):
    a = _service_record(db, case, document, section_label="Services section", minutes=30.0)
    b = _service_record(db, case, document, section_label="Summary section", minutes=20.0)
    db.commit()
    return a, b


def test_scan_creates_flag_for_mismatched_services(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a, b = _two_conflicting_records(db_session, sample_case, document)

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    assert len(created) == 1
    flag = created[0]
    assert flag.status == "pending"
    assert flag.case_id == sample_case.case_id
    assert flag.source_a_record_id == a.record_id
    assert flag.source_b_record_id == b.record_id
    assert flag.linked_timeline_event_id is None
    assert flag.linked_verified_fact_id is None


def test_scan_creates_no_flag_for_identical_services(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _service_record(db_session, sample_case, document, section_label="A", minutes=30.0)
    _service_record(db_session, sample_case, document, section_label="B", minutes=30.0)
    db_session.commit()

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    assert created == []


def test_rescan_is_idempotent_no_duplicate_flags(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)

    first = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    second = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    assert len(first) == 1
    assert second == []
    all_flags = db_session.scalars(select(IepInconsistencyFlag)).all()
    assert len(all_flags) == 1


def test_rescan_never_resurrects_a_confirmed_flag(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    confirm_flag(created[0], "reviewer")
    db_session.commit()

    second = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    assert second == []
    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.status == "confirmed"
    all_flags = db_session.scalars(select(IepInconsistencyFlag)).all()
    assert len(all_flags) == 1


def test_rescan_never_resurrects_a_dismissed_flag(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    dismiss_flag(created[0], "reviewer")
    db_session.commit()

    second = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    assert second == []
    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.status == "dismissed"


def test_a_genuinely_new_record_produces_a_new_flag_not_a_mutation(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a, b = _two_conflicting_records(db_session, sample_case, document)

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    original_dedup_key = created[0].dedup_key

    # A human adds a third, corrected manual entry for the same service.
    _service_record(db_session, sample_case, document, section_label="Corrected entry", minutes=25.0)
    db_session.commit()

    second = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    assert len(second) == 2  # (a vs c) and (b vs c), both new dedup_keys
    for flag in second:
        assert flag.dedup_key != original_dedup_key
    all_flags = db_session.scalars(select(IepInconsistencyFlag)).all()
    assert len(all_flags) == 3


def test_excluding_source_record_does_not_alter_existing_flag_snapshot(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a, b = _two_conflicting_records(db_session, sample_case, document)

    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()
    snapshot_before = dict(created[0].extracted_value_a)

    a.status = "excluded"
    db_session.commit()

    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.extracted_value_a == snapshot_before
    assert reloaded.status == "pending"  # excluding the source never changes the flag's own status


def test_scan_never_writes_to_iep_records_or_fields(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    a, b = _two_conflicting_records(db_session, sample_case, document)
    fields_before = {
        f.field_id: (f.text_value, f.numeric_value)
        for f in db_session.scalars(select(IepRecordField)).all()
    }

    scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    fields_after = {
        f.field_id: (f.text_value, f.numeric_value)
        for f in db_session.scalars(select(IepRecordField)).all()
    }
    assert fields_before == fields_after
    assert db_session.get(IepRecord, a.record_id).status == "active"
    assert db_session.get(IepRecord, b.record_id).status == "active"


def test_confirm_flag_sets_status_and_reviewer(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)
    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    confirm_flag(created[0], "reviewer-1")
    db_session.commit()

    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.status == "confirmed"
    assert reloaded.reviewed_by == "reviewer-1"
    assert reloaded.reviewed_at is not None


def test_dismiss_flag_sets_status_and_reviewer(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)
    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    dismiss_flag(created[0], "reviewer-1")
    db_session.commit()

    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.status == "dismissed"
    assert reloaded.reviewed_by == "reviewer-1"


def test_confirm_and_dismiss_are_reversible(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)
    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    confirm_flag(created[0], "reviewer-1")
    db_session.commit()
    assert db_session.get(IepInconsistencyFlag, created[0].flag_id).status == "confirmed"

    dismiss_flag(created[0], "reviewer-1")
    db_session.commit()
    assert db_session.get(IepInconsistencyFlag, created[0].flag_id).status == "dismissed"

    confirm_flag(created[0], "reviewer-1")
    db_session.commit()
    assert db_session.get(IepInconsistencyFlag, created[0].flag_id).status == "confirmed"


def test_set_flag_note_sets_and_clears(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document)
    created = scan_document_for_service_inconsistencies(db_session, sample_case, document, "test-user")
    db_session.commit()

    set_flag_note(created[0], "Confirmed with the district coordinator.")
    db_session.commit()
    reloaded = db_session.get(IepInconsistencyFlag, created[0].flag_id)
    assert reloaded.user_note == "Confirmed with the district coordinator."
    assert reloaded.status == "pending"  # a note never changes status

    set_flag_note(reloaded, None)
    db_session.commit()
    assert db_session.get(IepInconsistencyFlag, created[0].flag_id).user_note is None


def test_list_inconsistency_flags_returns_only_this_case(db_session: Session, sample_case: Case):
    other_case = Case(label="Other Student")
    db_session.add(other_case)
    db_session.flush()

    document_a = _document(db_session, sample_case)
    _two_conflicting_records(db_session, sample_case, document_a)
    scan_document_for_service_inconsistencies(db_session, sample_case, document_a, "test-user")
    db_session.commit()

    document_b = _document(db_session, other_case)
    _two_conflicting_records(db_session, other_case, document_b)
    scan_document_for_service_inconsistencies(db_session, other_case, document_b, "test-user")
    db_session.commit()

    flags = list_inconsistency_flags(db_session, sample_case.case_id)
    assert len(flags) == 1
    assert flags[0].case_id == sample_case.case_id
