"""Seed data — default lookup table rows for a freshly initialized vault."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnnotationType, DocumentType, EventType, FactType

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
