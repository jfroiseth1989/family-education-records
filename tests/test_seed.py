"""Tests for app/db/seed.py -- default lookup table rows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DocumentType, EventType, FactType
from app.db.seed import (
    DEFAULT_DOCUMENT_TYPES,
    DEFAULT_EVENT_TYPES,
    DEFAULT_FACT_TYPES,
    seed_document_types,
    seed_event_types,
    seed_fact_types,
)


def test_seed_document_types_inserts_defaults(db_session: Session):
    # db_session fixture already seeds once; assert it took effect.
    names = set(db_session.scalars(select(DocumentType.name)))
    assert names == {name for name, _ in DEFAULT_DOCUMENT_TYPES}


def test_seed_document_types_is_idempotent(db_session: Session):
    seed_document_types(db_session)
    seed_document_types(db_session)

    names = db_session.scalars(select(DocumentType.name)).all()
    assert len(names) == len(DEFAULT_DOCUMENT_TYPES)


# FERChronos Step 5.6 categories.
_NEW_DOCUMENT_TYPE_NAMES = [
    "Transportation Plan",
    "Functional Behavioral Assessment (FBA)",
    "Behavior Intervention Plan (BIP)",
    "Report Card",
    "Mediation",
    "Prior Written Notice",
    "Meeting Notice",
    "Consent Form",
    "Progress Report",
    "Service Log",
    "Therapy Record",
    "Manifestation Determination",
    "Restraint/Seclusion Record",
    "State Complaint",
    "OCR Complaint",
    "Due Process",
]


def test_default_document_types_has_no_duplicate_names():
    names = [name for name, _ in DEFAULT_DOCUMENT_TYPES]
    assert len(names) == len(set(names))


def test_every_new_step_5_6_category_present_exactly_once():
    names = [name for name, _ in DEFAULT_DOCUMENT_TYPES]
    for new_name in _NEW_DOCUMENT_TYPE_NAMES:
        assert names.count(new_name) == 1, f"{new_name!r} should appear exactly once"


def test_every_pre_existing_category_preserved():
    names = {name for name, _ in DEFAULT_DOCUMENT_TYPES}
    pre_existing = {
        "IEP",
        "504 Plan",
        "Evaluation",
        "Correspondence",
        "Discipline",
        "Attendance",
        "Grades",
        "Medical",
        "Legal Filing",
        "Audio Transcript",
    }
    assert pre_existing <= names


def test_other_is_the_last_document_type():
    names = [name for name, _ in DEFAULT_DOCUMENT_TYPES]
    assert names[-1] == "Other"


def test_seeded_document_types_include_new_categories(db_session: Session):
    names = set(db_session.scalars(select(DocumentType.name)))
    for new_name in _NEW_DOCUMENT_TYPE_NAMES:
        assert new_name in names


def test_seed_fact_types_inserts_defaults(db_session: Session):
    # db_session fixture already seeds once; assert it took effect.
    names = set(db_session.scalars(select(FactType.name)))
    assert names == {name for name, _ in DEFAULT_FACT_TYPES}


def test_seed_fact_types_is_idempotent(db_session: Session):
    seed_fact_types(db_session)
    seed_fact_types(db_session)

    names = db_session.scalars(select(FactType.name)).all()
    assert len(names) == len(DEFAULT_FACT_TYPES)


def test_seed_event_types_inserts_defaults(db_session: Session):
    # db_session fixture already seeds once; assert it took effect.
    names = set(db_session.scalars(select(EventType.name)))
    assert names == {name for name, _ in DEFAULT_EVENT_TYPES}


def test_seed_event_types_is_idempotent(db_session: Session):
    seed_event_types(db_session)
    seed_event_types(db_session)

    names = db_session.scalars(select(EventType.name)).all()
    assert len(names) == len(DEFAULT_EVENT_TYPES)
