"""Schema tests for IEP Consistency Review Step 1 (schema only).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2. No core module exists yet
(extraction/comparison start in Step 2) -- these tests exercise the ORM
models directly to confirm the schema itself (tables, foreign keys,
nullable-either-Document-or-Communication anchoring, the dedup-key
uniqueness constraint) is correct before any application logic is built
on top of it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Case,
    Citation,
    Communication,
    Document,
    IepDocumentLink,
    IepDocumentLinkType,
    IepFieldType,
    IepInconsistencyFlag,
    IepInconsistencyType,
    IepRecord,
    IepRecordField,
    IepRecordType,
)


def _document(db: Session, case: Case, filename: str = "iep.pdf", sha_prefix: str = "a") -> Document:
    document = Document(
        case_id=case.case_id,
        original_filename=filename,
        stored_path=f"cases/1/documents/{filename}",
        sha256_hash=sha_prefix * 64,
        file_size_bytes=100,
        ingested_by="test-user",
    )
    db.add(document)
    db.flush()
    return document


def _citation(db: Session, document: Document, quoted_text: str = "Speech-language therapy — 30 minutes, 2x/week") -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text=quoted_text)
    db.add(citation)
    db.flush()
    return citation


def _communication(db: Session, case: Case, sha_prefix: str = "b") -> Communication:
    communication = Communication(
        case_id=case.case_id,
        subject="Re: OT services",
        body_text="OT will now be 30 minutes, 2x weekly.",
        sha256_hash=sha_prefix * 64,
        stored_path="cases/1/communications/aa/message.eml",
        file_size_bytes=100,
        import_method="manual_upload",
        imported_by="test-user",
    )
    db.add(communication)
    db.flush()
    return communication


def _record_type(db: Session, name: str = "service") -> IepRecordType:
    return db.scalar(select(IepRecordType).where(IepRecordType.name == name))


def _field_type(db: Session, name: str) -> IepFieldType:
    return db.scalar(select(IepFieldType).where(IepFieldType.name == name))


def _inconsistency_type(db: Session, name: str = "service_minutes_mismatch") -> IepInconsistencyType:
    return db.scalar(select(IepInconsistencyType).where(IepInconsistencyType.name == name))


def _link_type(db: Session, name: str = "prior_iep_to_current_iep") -> IepDocumentLinkType:
    return db.scalar(select(IepDocumentLinkType).where(IepDocumentLinkType.name == name))


def test_document_sourced_record_with_typed_fields_and_citation(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    record_type = _record_type(db_session, "service")

    record = IepRecord(
        case_id=sample_case.case_id,
        document_id=document.document_id,
        record_type_id=record_type.type_id,
        section_label="Services table, page 4",
        comparison_key="speech-language therapy",
        extraction_method="manual",
        created_by="test-user",
    )
    db_session.add(record)
    db_session.flush()

    minutes_field = IepRecordField(
        record_id=record.record_id,
        field_type_id=_field_type(db_session, "minutes").type_id,
        numeric_value=30.0,
        citation_id=citation.citation_id,
    )
    frequency_count_field = IepRecordField(
        record_id=record.record_id,
        field_type_id=_field_type(db_session, "frequency_count").type_id,
        numeric_value=2.0,
        citation_id=citation.citation_id,
    )
    frequency_period_field = IepRecordField(
        record_id=record.record_id,
        field_type_id=_field_type(db_session, "frequency_period").type_id,
        text_value="week",
        citation_id=citation.citation_id,
    )
    db_session.add_all([minutes_field, frequency_count_field, frequency_period_field])
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert stored.status == "active"
    assert stored.document_id == document.document_id
    assert stored.communication_id is None
    assert len(stored.fields) == 3

    stored_minutes = db_session.get(IepRecordField, minutes_field.field_id)
    assert stored_minutes.numeric_value == 30.0
    assert stored_minutes.text_value is None
    assert stored_minutes.citation.quoted_text.startswith("Speech-language therapy")
    assert stored_minutes.field_type.value_kind == "number"


def test_communication_sourced_record_has_no_citation(db_session: Session, sample_case: Case):
    communication = _communication(db_session, sample_case)
    record_type = _record_type(db_session, "service")

    record = IepRecord(
        case_id=sample_case.case_id,
        communication_id=communication.communication_id,
        record_type_id=record_type.type_id,
        extraction_method="iep-service-line-regex-v1",
        created_by="system",
    )
    db_session.add(record)
    db_session.flush()

    field = IepRecordField(
        record_id=record.record_id,
        field_type_id=_field_type(db_session, "minutes").type_id,
        numeric_value=30.0,
        citation_id=None,
    )
    db_session.add(field)
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert stored.document_id is None
    assert stored.communication_id == communication.communication_id
    assert stored.fields[0].citation_id is None
    assert stored.fields[0].citation is None


def test_record_excluded_status_never_deletes_the_row(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    record = IepRecord(
        case_id=sample_case.case_id,
        document_id=document.document_id,
        record_type_id=_record_type(db_session, "service").type_id,
        extraction_method="manual",
        created_by="test-user",
    )
    db_session.add(record)
    db_session.flush()
    db_session.add(
        IepRecordField(
            record_id=record.record_id,
            field_type_id=_field_type(db_session, "minutes").type_id,
            numeric_value=30.0,
            citation_id=citation.citation_id,
        )
    )
    db_session.commit()

    record.status = "excluded"
    db_session.commit()

    stored = db_session.get(IepRecord, record.record_id)
    assert stored is not None
    assert stored.status == "excluded"
    assert len(stored.fields) == 1


def test_document_link_pending_review_default_and_confirm(db_session: Session, sample_case: Case):
    prior = _document(db_session, sample_case, filename="iep-2023.pdf", sha_prefix="a")
    current = _document(db_session, sample_case, filename="iep-2024.pdf", sha_prefix="c")

    link = IepDocumentLink(
        case_id=sample_case.case_id,
        link_type_id=_link_type(db_session, "prior_iep_to_current_iep").type_id,
        from_document_id=prior.document_id,
        to_document_id=current.document_id,
        method="same-case-chronological-iep-heuristic-v1",
        created_by="system",
    )
    db_session.add(link)
    db_session.commit()

    stored = db_session.get(IepDocumentLink, link.link_id)
    assert stored.status == "pending_review"
    assert stored.reviewed_by is None
    assert stored.from_document.document_id == prior.document_id
    assert stored.to_document.document_id == current.document_id
    assert stored.to_communication is None

    stored.status = "confirmed"
    stored.reviewed_by = "test-user"
    db_session.commit()

    reloaded = db_session.get(IepDocumentLink, link.link_id)
    assert reloaded.status == "confirmed"


def test_document_link_to_communication_instead_of_document(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    communication = _communication(db_session, sample_case)

    link = IepDocumentLink(
        case_id=sample_case.case_id,
        link_type_id=_link_type(db_session, "related_communication").type_id,
        from_document_id=document.document_id,
        to_communication_id=communication.communication_id,
        method="manual",
        created_by="test-user",
    )
    db_session.add(link)
    db_session.commit()

    stored = db_session.get(IepDocumentLink, link.link_id)
    assert stored.to_document_id is None
    assert stored.to_communication.communication_id == communication.communication_id


def _two_service_records(db_session: Session, case: Case, document: Document, citation: Citation):
    record_type = _record_type(db_session, "service")
    record_a = IepRecord(
        case_id=case.case_id,
        document_id=document.document_id,
        record_type_id=record_type.type_id,
        section_label="Services table, page 4",
        comparison_key="speech-language therapy",
        extraction_method="manual",
        created_by="test-user",
    )
    record_b = IepRecord(
        case_id=case.case_id,
        document_id=document.document_id,
        record_type_id=record_type.type_id,
        section_label="Summary page, page 9",
        comparison_key="speech-language therapy",
        extraction_method="manual",
        created_by="test-user",
    )
    db_session.add_all([record_a, record_b])
    db_session.flush()

    field_a = IepRecordField(
        record_id=record_a.record_id,
        field_type_id=_field_type(db_session, "minutes").type_id,
        numeric_value=30.0,
        citation_id=citation.citation_id,
    )
    field_b = IepRecordField(
        record_id=record_b.record_id,
        field_type_id=_field_type(db_session, "minutes").type_id,
        numeric_value=20.0,
        citation_id=citation.citation_id,
    )
    db_session.add_all([field_a, field_b])
    db_session.flush()
    return record_a, field_a, record_b, field_b


def test_inconsistency_flag_round_trips_with_snapshot_and_lifecycle(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    record_a, field_a, record_b, field_b = _two_service_records(db_session, sample_case, document, citation)

    flag = IepInconsistencyFlag(
        case_id=sample_case.case_id,
        inconsistency_type_id=_inconsistency_type(db_session, "service_minutes_mismatch").type_id,
        comparison_mode="within_document",
        source_a_record_id=record_a.record_id,
        source_a_field_id=field_a.field_id,
        source_b_record_id=record_b.record_id,
        source_b_field_id=field_b.field_id,
        rule_id="service_schedule_mismatch_v1",
        reason_text="Frequency and duration do not match.",
        extracted_value_a={"minutes": 30.0, "section_label": "Services table, page 4"},
        extracted_value_b={"minutes": 20.0, "section_label": "Summary page, page 9"},
        dedup_key="deadbeef" * 8,
    )
    db_session.add(flag)
    db_session.commit()

    stored = db_session.get(IepInconsistencyFlag, flag.flag_id)
    assert stored.status == "pending"
    assert stored.reviewed_by is None
    assert stored.extracted_value_a["minutes"] == 30.0
    assert stored.extracted_value_b["minutes"] == 20.0
    assert stored.source_a_record.record_id == record_a.record_id
    assert stored.source_b_field.field_id == field_b.field_id

    stored.status = "confirmed"
    stored.reviewed_by = "test-user"
    stored.user_note = "Confirmed with the district's IEP coordinator."
    db_session.commit()

    reloaded = db_session.get(IepInconsistencyFlag, flag.flag_id)
    assert reloaded.status == "confirmed"
    assert reloaded.user_note == "Confirmed with the district's IEP coordinator."


def test_inconsistency_flag_dedup_key_unique_per_case(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    record_a, field_a, record_b, field_b = _two_service_records(db_session, sample_case, document, citation)
    inconsistency_type = _inconsistency_type(db_session, "service_minutes_mismatch")

    flag_kwargs = dict(
        case_id=sample_case.case_id,
        inconsistency_type_id=inconsistency_type.type_id,
        comparison_mode="within_document",
        source_a_record_id=record_a.record_id,
        source_a_field_id=field_a.field_id,
        source_b_record_id=record_b.record_id,
        source_b_field_id=field_b.field_id,
        rule_id="service_schedule_mismatch_v1",
        reason_text="Frequency and duration do not match.",
        dedup_key="samekey" * 9,
    )
    db_session.add(IepInconsistencyFlag(**flag_kwargs))
    db_session.commit()

    db_session.add(IepInconsistencyFlag(**flag_kwargs))
    try:
        db_session.commit()
        raised = False
    except IntegrityError:
        db_session.rollback()
        raised = True
    assert raised, "duplicate (case_id, dedup_key) must be rejected at the database level"


def test_inconsistency_flag_can_link_to_timeline_event_and_verified_fact(db_session: Session, sample_case: Case):
    from app.core.facts.service import create_verified_fact
    from app.core.timeline.service import create_timeline_event

    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    record_a, field_a, record_b, field_b = _two_service_records(db_session, sample_case, document, citation)

    fact = create_verified_fact(
        db_session,
        sample_case,
        "date",
        "IEP meeting held",
        "certain",
        [citation.citation_id],
        "test-user",
        fact_date=citation.document.ingested_at,
    )
    db_session.flush()
    event = create_timeline_event(db_session, sample_case, "meeting", "IEP Meeting", fact.fact_id, "test-user")
    db_session.commit()

    flag = IepInconsistencyFlag(
        case_id=sample_case.case_id,
        inconsistency_type_id=_inconsistency_type(db_session, "service_minutes_mismatch").type_id,
        comparison_mode="within_document",
        source_a_record_id=record_a.record_id,
        source_a_field_id=field_a.field_id,
        source_b_record_id=record_b.record_id,
        source_b_field_id=field_b.field_id,
        rule_id="service_schedule_mismatch_v1",
        reason_text="Frequency and duration do not match.",
        dedup_key="linktest" * 8,
        linked_timeline_event_id=event.event_id,
        linked_verified_fact_id=fact.fact_id,
    )
    db_session.add(flag)
    db_session.commit()

    stored = db_session.get(IepInconsistencyFlag, flag.flag_id)
    assert stored.linked_timeline_event.event_id == event.event_id
    assert stored.linked_verified_fact.fact_id == fact.fact_id
