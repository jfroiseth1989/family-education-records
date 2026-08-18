"""Shared lookup-row fetch helpers for the IEP Consistency Review core
modules.

Mirrors app/core/facts/service.py::_get_fact_type() and
app/core/timeline/service.py::_get_event_type() -- a small, explicit
"fetch this lookup row by name or raise" helper per lookup table,
rather than duplicating the same select-and-check ad hoc in every
caller.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    IepDocumentLinkType,
    IepFieldType,
    IepInconsistencyType,
    IepRecordType,
)


def get_record_type(db: Session, name: str) -> IepRecordType:
    record_type = db.scalars(select(IepRecordType).where(IepRecordType.name == name)).one_or_none()
    if record_type is None:
        raise ValueError(f"Unknown IEP record type '{name}'.")
    return record_type


def get_field_type(db: Session, name: str) -> IepFieldType:
    field_type = db.scalars(select(IepFieldType).where(IepFieldType.name == name)).one_or_none()
    if field_type is None:
        raise ValueError(f"Unknown IEP field type '{name}'.")
    return field_type


def get_inconsistency_type(db: Session, name: str) -> IepInconsistencyType:
    inconsistency_type = db.scalars(
        select(IepInconsistencyType).where(IepInconsistencyType.name == name)
    ).one_or_none()
    if inconsistency_type is None:
        raise ValueError(f"Unknown IEP inconsistency type '{name}'.")
    return inconsistency_type


def get_document_link_type(db: Session, name: str) -> IepDocumentLinkType:
    link_type = db.scalars(
        select(IepDocumentLinkType).where(IepDocumentLinkType.name == name)
    ).one_or_none()
    if link_type is None:
        raise ValueError(f"Unknown IEP document link type '{name}'.")
    return link_type
