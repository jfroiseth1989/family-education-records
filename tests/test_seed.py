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
