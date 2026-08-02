"""Schema tests for the Phase 4 Step 1 timeline tables.

See docs/ARCHITECTURE.md §3.8 and docs/PHASE_4_IMPLEMENTATION_PLAN.md
§1/§2/§3 Step 1. No core module or UI exists yet (that's Steps 2-3) --
these tests exercise the ORM models directly to confirm the schema
itself (tables, foreign keys, the one-date-source-fact-per-event
convention) is correct before any application logic is built on top of it.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Case,
    Citation,
    Document,
    EventType,
    FactType,
    TimelineEvent,
    TimelineEventFact,
    VerifiedFact,
)

_MARCH_12 = datetime.datetime(2024, 3, 12, tzinfo=datetime.timezone.utc)


def _document(db: Session, case: Case) -> Document:
    document = Document(
        case_id=case.case_id,
        original_filename="letter.pdf",
        stored_path="cases/1/documents/letter.pdf",
        sha256_hash="a" * 64,
        file_size_bytes=100,
        ingested_by="test-user",
    )
    db.add(document)
    db.flush()
    return document


def _citation(db: Session, document: Document, quoted_text: str = "IEP meeting held March 12, 2024") -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text=quoted_text)
    db.add(citation)
    db.flush()
    return citation


def _date_fact(db: Session, case: Case, citation: Citation, fact_date: datetime.datetime = _MARCH_12) -> VerifiedFact:
    fact_type = db.scalars(select(FactType).where(FactType.name == "date")).one()
    fact = VerifiedFact(
        case_id=case.case_id,
        fact_type_id=fact_type.type_id,
        statement="IEP meeting held",
        confidence_label="certain",
        fact_date=fact_date,
        created_by="test-user",
    )
    db.add(fact)
    db.flush()
    return fact


def _event_type(db: Session, name: str = "meeting") -> EventType:
    return db.scalars(select(EventType).where(EventType.name == name)).one()


def test_timeline_event_round_trips_with_date_source_fact(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    date_fact = _date_fact(db_session, sample_case, citation)
    event_type = _event_type(db_session)

    event = TimelineEvent(
        case_id=sample_case.case_id,
        event_date=date_fact.fact_date,
        event_date_precision="exact",
        event_date_source="verified_fact",
        title="IEP annual review meeting",
        event_type_id=event_type.type_id,
        created_by="manual",
        status="confirmed",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(TimelineEventFact(event_id=event.event_id, fact_id=date_fact.fact_id, is_date_source=True))
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored.event_date == _MARCH_12
    assert stored.title == "IEP annual review meeting"
    assert stored.created_by == "manual"
    assert stored.status == "confirmed"
    assert stored.deleted_at is None

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 1
    assert links[0].fact_id == date_fact.fact_id
    assert links[0].is_date_source is True


def test_timeline_event_can_have_supporting_facts_too(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation_a = _citation(db_session, document, "IEP meeting held March 12, 2024")
    citation_b = _citation(db_session, document, "Attendees: parent, teacher, case manager")
    date_fact = _date_fact(db_session, sample_case, citation_a)

    fact_type = db_session.scalars(select(FactType).where(FactType.name == "category")).one()
    supporting_fact = VerifiedFact(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="Attendees included parent, teacher, and case manager",
        confidence_label="certain",
        created_by="test-user",
    )
    db_session.add(supporting_fact)
    db_session.flush()

    event_type = _event_type(db_session)
    event = TimelineEvent(
        case_id=sample_case.case_id,
        event_date=date_fact.fact_date,
        event_date_precision="exact",
        event_date_source="verified_fact",
        title="IEP annual review meeting",
        event_type_id=event_type.type_id,
        created_by="manual",
        status="confirmed",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(TimelineEventFact(event_id=event.event_id, fact_id=date_fact.fact_id, is_date_source=True))
    db_session.add(TimelineEventFact(event_id=event.event_id, fact_id=supporting_fact.fact_id, is_date_source=False))
    db_session.commit()

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 2
    date_source_links = [link for link in links if link.is_date_source]
    assert len(date_source_links) == 1
    assert date_source_links[0].fact_id == date_fact.fact_id


def test_timeline_event_soft_delete_leaves_row_in_place(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    date_fact = _date_fact(db_session, sample_case, citation)
    event_type = _event_type(db_session)

    event = TimelineEvent(
        case_id=sample_case.case_id,
        event_date=date_fact.fact_date,
        event_date_precision="exact",
        event_date_source="verified_fact",
        title="IEP annual review meeting",
        event_type_id=event_type.type_id,
        created_by="manual",
        status="confirmed",
    )
    db_session.add(event)
    db_session.commit()

    event.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored is not None
    assert stored.title == "IEP annual review meeting"
    assert stored.deleted_at is not None


def test_timeline_event_range_precision_stores_range_end(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    date_fact = _date_fact(db_session, sample_case, citation)
    event_type = _event_type(db_session)

    range_end = datetime.datetime(2024, 3, 20, tzinfo=datetime.timezone.utc)
    event = TimelineEvent(
        case_id=sample_case.case_id,
        event_date=date_fact.fact_date,
        event_date_range_end=range_end,
        event_date_precision="range",
        event_date_source="verified_fact",
        title="Evaluation window",
        event_type_id=event_type.type_id,
        created_by="manual",
        status="confirmed",
    )
    db_session.add(event)
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored.event_date_precision == "range"
    assert stored.event_date_range_end == range_end
