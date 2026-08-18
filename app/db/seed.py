"""Seed data — default lookup table rows for a freshly initialized vault."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AnnotationType,
    DocumentType,
    EventType,
    FactType,
    IepDocumentLinkType,
    IepFieldType,
    IepInconsistencyType,
    IepRecordType,
)

# Matches the default list in docs/DATA_MODEL.md "documents". Users are not
# limited to this list — DocumentType is a lookup table specifically so
# more rows can be added later without a migration (see
# docs/DATA_MODEL.md "Extensibility"); Phase 1 just doesn't expose a UI for
# adding one yet.
DEFAULT_DOCUMENT_TYPES: list[tuple[str, str]] = [
    ("IEP", "Individualized Education Program"),
    ("504 Plan", "Section 504 accommodation plan"),
    ("Evaluation", "Educational, psychological, or related evaluation report"),
    ("Correspondence", "Letters, emails, or other written communication"),
    ("Discipline", "Disciplinary records or notices"),
    ("Attendance", "Attendance records"),
    ("Grades", "Report cards, transcripts, or grade reports"),
    ("Medical", "Medical or health-related records"),
    ("Legal Filing", "Legal filings, due process documents, or related correspondence"),
    ("Audio Transcript", "Transcript of an audio or video recording"),
    # Added in FERChronos Step 5.6 -- see docs/DATA_MODEL.md "Extensibility"
    # and app/core/document_type_suggestion.py, whose trigger phrases match
    # these names exactly.
    ("Transportation Plan", "A student's transportation accommodations or arrangements"),
    ("Functional Behavioral Assessment (FBA)", "An assessment of the function behind a behavior"),
    ("Behavior Intervention Plan (BIP)", "A plan addressing a specific behavior"),
    ("Report Card", "A grading-period report card"),
    ("Mediation", "Mediation agreements, requests, or session records"),
    ("Prior Written Notice", "Prior written notice of a proposed or refused action"),
    ("Meeting Notice", "Notice of an upcoming IEP, 504, or other meeting"),
    ("Consent Form", "A parental consent or authorization form"),
    ("Progress Report", "A periodic progress report on IEP/504 goals"),
    ("Service Log", "A log of related services delivered (e.g. speech, OT, PT)"),
    ("Therapy Record", "Notes or records from a therapy session"),
    ("Manifestation Determination", "A manifestation determination review record"),
    ("Restraint/Seclusion Record", "A record of a restraint or seclusion incident"),
    ("State Complaint", "A state-level special education complaint"),
    ("OCR Complaint", "An Office for Civil Rights complaint"),
    ("Due Process", "A due process complaint, hearing request, or related record"),
    ("Other", "Anything that doesn't fit another category"),
]

# Matches docs/DATA_MODEL.md "annotation_types" -- the three annotation
# kinds designed in Phase 2 Step 4 (docs/PHASE_2_PLAN.md §7).
DEFAULT_ANNOTATION_TYPES: list[tuple[str, str]] = [
    ("highlight", "An exact, offset-anchored span of a page's text"),
    ("note", "A free-text note attached to a document or page"),
    ("bookmark", "A page-level marker with no required text"),
]

# Matches the example list in docs/DATA_MODEL.md "fact_types" -- shared
# by both `verified_facts` and `ai_observations` (Phase 3.5 Step 1).
# Same extensibility pattern as document/annotation types: more rows can
# be added later without a migration.
DEFAULT_FACT_TYPES: list[tuple[str, str]] = [
    ("date", "A specific date or date range asserted about the case"),
    ("person", "A person's identity, role, or involvement asserted about the case"),
    ("decision", "A decision, determination, or action taken by a party"),
    ("category", "A categorical claim not covered by another fact type"),
    ("custom", "A claim that doesn't fit another fact type"),
]

# Matches the example list in docs/DATA_MODEL.md "event_types" (Phase 4
# Step 1). Same extensibility pattern as the other lookup tables.
DEFAULT_EVENT_TYPES: list[tuple[str, str]] = [
    ("meeting", "An IEP, 504, or other meeting"),
    ("evaluation", "An educational, psychological, or related evaluation"),
    ("incident", "A disciplinary or safety incident"),
    ("communication", "A letter, email, or other communication"),
    ("decision", "A determination or decision made by a party"),
    ("deadline", "A deadline or due date"),
    ("other", "Anything that doesn't fit another category"),
]


# Matches docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.2 -- the extensible
# lookup tables for the IEP Consistency Review feature (Step 1: schema
# only, no extraction/comparison code reads these yet). Same
# row-insert-not-migration extensibility as every other lookup table here.
DEFAULT_IEP_RECORD_TYPES: list[tuple[str, str]] = [
    ("service", "A related service (e.g. speech, OT, PT, counseling) listed for a student"),
    ("goal", "A measurable annual goal"),
    ("accommodation", "An accommodation provided during instruction/testing"),
    ("modification", "A modification to curriculum or expectations"),
    ("assistive_technology", "An assistive technology device or service"),
    ("transportation_support", "A transportation-related support or accommodation"),
    ("present_level_need", "An identified area of need from present levels of performance"),
    ("disability_eligibility", "A stated disability category or eligibility determination"),
    ("evaluation_finding", "A finding or result stated in an evaluation report"),
    ("pwn_decision", "A proposed or refused action described in a Prior Written Notice"),
    ("parent_concern", "A parent/guardian concern documented in a source"),
]

# (name, value_kind, description) -- value_kind is text/number/date, telling
# the future comparison engine which of text_value/numeric_value/date_value
# on a field row is the one that matters.
DEFAULT_IEP_FIELD_TYPES: list[tuple[str, str, str]] = [
    ("service_name", "text", "The name of a related service"),
    ("provider", "text", "Who or what role provides a service"),
    ("minutes", "number", "Minutes of service per session"),
    ("frequency_count", "number", "How many times per period a service is provided"),
    ("frequency_period", "text", "The period a frequency count is measured against (e.g. week, month)"),
    ("duration_weeks", "number", "How many weeks a service/goal is in effect"),
    ("location", "text", "Where a service is delivered"),
    ("start_date", "date", "When a service or provision begins"),
    ("end_date", "date", "When a service or provision ends"),
    ("goal_area", "text", "The area a goal addresses (e.g. reading fluency)"),
    ("goal_identifier", "text", "A goal's own label or number, if the source gives one"),
    ("baseline_text", "text", "A goal's stated baseline/present performance"),
    ("target_text", "text", "A goal's stated measurable target"),
    ("accommodation_text", "text", "The text of one accommodation"),
    ("modification_text", "text", "The text of one modification"),
    ("disability_category", "text", "A stated disability category"),
    ("eligibility_status", "text", "A stated eligibility determination"),
    ("finding_text", "text", "The text of one evaluation finding"),
    ("decision_text", "text", "The text of one PWN-described decision"),
    ("concern_text", "text", "The text of one documented parent/guardian concern"),
    ("assistive_technology_text", "text", "The text describing an assistive technology item"),
    ("transportation_text", "text", "The text describing a transportation support"),
    ("iep_meeting_date", "date", "The IEP meeting date stated in a document"),
    ("iep_effective_start_date", "date", "When an IEP's provisions take effect"),
    ("iep_effective_end_date", "date", "When an IEP's provisions end"),
]

DEFAULT_IEP_INCONSISTENCY_TYPES: list[tuple[str, str]] = [
    ("service_minutes_mismatch", "The same service's stated minutes differ between two sources"),
    ("service_frequency_mismatch", "The same service's stated frequency differs between two sources"),
    ("service_location_mismatch", "The same service's stated location differs between two sources"),
    ("service_provider_mismatch", "The same service's stated provider differs between two sources"),
    ("duplicate_record_conflicting_field", "The same record appears twice with a differing structured field"),
    ("goal_missing_for_need", "No goal was found matching an identified area of need"),
    ("goal_missing_baseline", "A goal has no stated baseline"),
    ("goal_missing_target", "A goal has no stated measurable target"),
    ("accommodation_added", "An accommodation appears in the later source but not the earlier one"),
    ("accommodation_removed", "An accommodation appears in the earlier source but not the later one"),
    ("date_conflict", "The same date-type field disagrees between two extractions"),
    ("eligibility_mismatch", "A stated disability/eligibility label differs between two sources"),
    ("pwn_iep_mismatch", "A service described in a Prior Written Notice differs from the resulting IEP"),
    ("field_changed_between_versions", "A structured field's value changed between two linked versions"),
]

DEFAULT_IEP_DOCUMENT_LINK_TYPES: list[tuple[str, str]] = [
    ("prior_iep_to_current_iep", "This IEP is the chronologically prior version of that IEP"),
    ("annual_iep_to_amendment", "This document is an amendment of that annual IEP"),
    ("amendment_to_final_iep", "This amendment resulted in that final IEP"),
    ("evaluation_for", "This Evaluation informed that IEP"),
    ("eligibility_determination_for", "This Eligibility Determination informed that IEP"),
    ("pwn_for", "This Prior Written Notice relates to that IEP"),
    ("progress_report_for", "This Progress Report relates to that IEP"),
    ("related_communication", "This communication relates to that document"),
]


def seed_document_types(db: Session) -> None:
    """Insert the default document types if they don't already exist.

    Idempotent by design — safe to call on every application startup
    rather than needing a separate "have we seeded yet" flag.
    """
    existing_names = set(db.scalars(select(DocumentType.name)))
    for name, description in DEFAULT_DOCUMENT_TYPES:
        if name not in existing_names:
            db.add(DocumentType(name=name, description=description))
    db.commit()


def seed_annotation_types(db: Session) -> None:
    """Insert the default annotation types if they don't already exist.

    Idempotent, same pattern as seed_document_types().
    """
    existing_names = set(db.scalars(select(AnnotationType.name)))
    for name, description in DEFAULT_ANNOTATION_TYPES:
        if name not in existing_names:
            db.add(AnnotationType(name=name, description=description))
    db.commit()


def seed_fact_types(db: Session) -> None:
    """Insert the default fact types if they don't already exist.

    Idempotent, same pattern as seed_document_types().
    """
    existing_names = set(db.scalars(select(FactType.name)))
    for name, description in DEFAULT_FACT_TYPES:
        if name not in existing_names:
            db.add(FactType(name=name, description=description))
    db.commit()


def seed_event_types(db: Session) -> None:
    """Insert the default event types if they don't already exist.

    Idempotent, same pattern as seed_document_types().
    """
    existing_names = set(db.scalars(select(EventType.name)))
    for name, description in DEFAULT_EVENT_TYPES:
        if name not in existing_names:
            db.add(EventType(name=name, description=description))
    db.commit()


def seed_iep_record_types(db: Session) -> None:
    """Insert the default IEP record types if they don't already exist.

    Idempotent, same pattern as seed_document_types(). See
    docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.2.
    """
    existing_names = set(db.scalars(select(IepRecordType.name)))
    for name, description in DEFAULT_IEP_RECORD_TYPES:
        if name not in existing_names:
            db.add(IepRecordType(name=name, description=description))
    db.commit()


def seed_iep_field_types(db: Session) -> None:
    """Insert the default IEP field types if they don't already exist.

    Idempotent, same pattern as seed_document_types(). See
    docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.2.
    """
    existing_names = set(db.scalars(select(IepFieldType.name)))
    for name, value_kind, description in DEFAULT_IEP_FIELD_TYPES:
        if name not in existing_names:
            db.add(IepFieldType(name=name, value_kind=value_kind, description=description))
    db.commit()


def seed_iep_inconsistency_types(db: Session) -> None:
    """Insert the default IEP inconsistency types if they don't already exist.

    Idempotent, same pattern as seed_document_types(). See
    docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.2.
    """
    existing_names = set(db.scalars(select(IepInconsistencyType.name)))
    for name, description in DEFAULT_IEP_INCONSISTENCY_TYPES:
        if name not in existing_names:
            db.add(IepInconsistencyType(name=name, description=description))
    db.commit()


def seed_iep_document_link_types(db: Session) -> None:
    """Insert the default IEP document link types if they don't already exist.

    Idempotent, same pattern as seed_document_types(). See
    docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.2.
    """
    existing_names = set(db.scalars(select(IepDocumentLinkType.name)))
    for name, description in DEFAULT_IEP_DOCUMENT_LINK_TYPES:
        if name not in existing_names:
            db.add(IepDocumentLinkType(name=name, description=description))
    db.commit()
