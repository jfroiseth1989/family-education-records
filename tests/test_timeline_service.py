"""Tests for app/core/timeline/service.py -- Phase 4 Step 2.

See docs/ARCHITECTURE.md §3.8 and docs/PHASE_4_IMPLEMENTATION_PLAN.md §2/§4.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.facts.service import create_verified_fact
from app.core.timeline.service import (
    attach_fact_to_event,
    compute_date_gaps,
    create_timeline_event,
    list_timeline_events,
    remove_timeline_event,
)
from app.db.models import (
    AuditLog,
    Case,
    Citation,
    Document,
    EventType,
    FactType,
    TimelineEvent,
    TimelineEventFact,
    VerifiedFact,
)

_MARCH_12 = datetime(2024, 3, 12, tzinfo=timezone.utc)
_APRIL_1 = datetime(2024, 4, 1, tzinfo=timezone.utc)
_MARCH_20 = datetime(2024, 3, 20, tzinfo=timezone.utc)

_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"letter-{_document_counter}.pdf"
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


def _citation(db: Session, document: Document, quoted_text: str = "IEP meeting held March 12, 2024") -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text=quoted_text)
    db.add(citation)
    db.flush()
    return citation


def _date_fact(db: Session, case: Case, fact_date: datetime = _MARCH_12) -> VerifiedFact:
    document = _document(db, case)
    citation = _citation(db, document)
    return create_verified_fact(
        db, case, "date", "IEP meeting held", "certain",
        [citation.citation_id], actor="test-user", fact_date=fact_date,
    )


def _supporting_fact(db: Session, case: Case, statement: str = "Attendees noted") -> VerifiedFact:
    document = _document(db, case)
    citation = _citation(db, document, statement)
    return create_verified_fact(
        db, case, "category", statement, "certain", [citation.citation_id], actor="test-user",
    )


def _event_type_id(db: Session, name: str = "meeting") -> int:
    return db.scalars(select(EventType.type_id).where(EventType.name == name)).one()


# --- create_timeline_event --------------------------------------------------


def test_create_timeline_event_succeeds(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    event = create_timeline_event(
        db_session, sample_case, "meeting", "IEP annual review meeting",
        date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored.event_date == _MARCH_12
    assert stored.title == "IEP annual review meeting"
    assert stored.created_by == "manual"
    assert stored.status == "confirmed"
    assert stored.event_date_source == "verified_fact"
    assert stored.deleted_at is None

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 1
    assert links[0].fact_id == date_fact.fact_id
    assert links[0].is_date_source is True

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "timeline_event_created")
    ).all()
    assert len(entries) == 1
    assert entries[0].entity_id == event.event_id


def test_create_timeline_event_with_supporting_facts(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    support = _supporting_fact(db_session, sample_case)
    db_session.commit()

    event = create_timeline_event(
        db_session, sample_case, "meeting", "IEP annual review meeting",
        date_fact.fact_id, actor="test-user", additional_fact_ids=[support.fact_id],
    )
    db_session.commit()

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 2
    by_fact = {link.fact_id: link.is_date_source for link in links}
    assert by_fact[date_fact.fact_id] is True
    assert by_fact[support.fact_id] is False


def test_create_timeline_event_dedups_date_fact_from_additional_ids(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    event = create_timeline_event(
        db_session, sample_case, "meeting", "IEP annual review meeting",
        date_fact.fact_id, actor="test-user", additional_fact_ids=[date_fact.fact_id],
    )
    db_session.commit()

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 1


def test_create_timeline_event_rejects_empty_title(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="Title cannot be empty"):
        create_timeline_event(db_session, sample_case, "meeting", "   ", date_fact.fact_id, actor="test-user")


def test_create_timeline_event_rejects_non_date_fact_as_date_source(db_session: Session, sample_case: Case):
    support = _supporting_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="cannot anchor a timeline event"):
        create_timeline_event(db_session, sample_case, "meeting", "A meeting", support.fact_id, actor="test-user")


def test_create_timeline_event_rejects_date_fact_with_no_fact_date(db_session: Session, sample_case: Case):
    # Simulates a corrupt/impossible state directly via the ORM, since
    # create_verified_fact() itself refuses to create this combination.
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact_type = db_session.scalars(select(FactType).where(FactType.name == "date")).one()
    bad_fact = VerifiedFact(
        case_id=sample_case.case_id, fact_type_id=fact_type.type_id, statement="Bad fact",
        confidence_label="certain", created_by="test-user",
    )
    db_session.add(bad_fact)
    db_session.commit()

    with pytest.raises(ValueError, match="cannot anchor a timeline event"):
        create_timeline_event(db_session, sample_case, "meeting", "A meeting", bad_fact.fact_id, actor="test-user")


def test_create_timeline_event_rejects_fact_from_another_case(db_session: Session, sample_case: Case):
    other_case = Case(label="Other Case")
    db_session.add(other_case)
    db_session.flush()
    other_date_fact = _date_fact(db_session, other_case)
    db_session.commit()

    with pytest.raises(ValueError, match="different case"):
        create_timeline_event(
            db_session, sample_case, "meeting", "A meeting", other_date_fact.fact_id, actor="test-user",
        )


def test_create_timeline_event_rejects_unknown_event_type(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="Unknown event type"):
        create_timeline_event(
            db_session, sample_case, "not-a-real-type", "A meeting", date_fact.fact_id, actor="test-user",
        )


def test_create_timeline_event_range_precision_requires_range_end(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="event_date_range_end is required"):
        create_timeline_event(
            db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
            event_date_precision="range",
        )


def test_create_timeline_event_range_end_before_event_date_rejected(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_20)
    db_session.commit()

    with pytest.raises(ValueError, match="on or after"):
        create_timeline_event(
            db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
            event_date_precision="range", event_date_range_end=_MARCH_12,
        )


def test_create_timeline_event_non_range_precision_rejects_range_end(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="not 'range'"):
        create_timeline_event(
            db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
            event_date_precision="exact", event_date_range_end=_APRIL_1,
        )


def test_create_timeline_event_range_precision_succeeds(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()

    event = create_timeline_event(
        db_session, sample_case, "evaluation", "Evaluation window", date_fact.fact_id, actor="test-user",
        event_date_precision="range", event_date_range_end=_MARCH_20,
    )
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored.event_date_precision == "range"
    assert stored.event_date_range_end == _MARCH_20


# --- attach_fact_to_event -----------------------------------------------


def test_attach_fact_to_event_succeeds(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    support = _supporting_fact(db_session, sample_case)
    db_session.commit()

    attach_fact_to_event(db_session, event, support.fact_id, actor="test-user")
    db_session.commit()

    links = db_session.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event.event_id)
    ).all()
    assert len(links) == 2

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "timeline_event_fact_attached")
    ).all()
    assert len(entries) == 1
    assert entries[0].details["fact_id"] == support.fact_id


def test_attach_fact_to_event_rejects_deleted_event(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()
    remove_timeline_event(db_session, event, actor="test-user")
    db_session.commit()

    support = _supporting_fact(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="has been deleted"):
        attach_fact_to_event(db_session, event, support.fact_id, actor="test-user")


def test_attach_fact_to_event_rejects_duplicate_attachment(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    with pytest.raises(ValueError, match="already attached"):
        attach_fact_to_event(db_session, event, date_fact.fact_id, actor="test-user")


def test_attach_fact_to_event_rejects_fact_from_another_case(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    other_case = Case(label="Other Case")
    db_session.add(other_case)
    db_session.flush()
    other_fact = _supporting_fact(db_session, other_case)
    db_session.commit()

    with pytest.raises(ValueError, match="different case"):
        attach_fact_to_event(db_session, event, other_fact.fact_id, actor="test-user")


# --- remove_timeline_event -----------------------------------------------


def test_remove_timeline_event_soft_deletes(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    remove_timeline_event(db_session, event, actor="test-user")
    db_session.commit()

    stored = db_session.get(TimelineEvent, event.event_id)
    assert stored is not None
    assert stored.deleted_at is not None

    # The underlying fact and its citation are never touched.
    stored_fact = db_session.get(VerifiedFact, date_fact.fact_id)
    assert stored_fact.deleted_at is None

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "timeline_event_deleted")
    ).all()
    assert len(entries) == 1


def test_remove_timeline_event_is_idempotent(db_session: Session, sample_case: Case):
    date_fact = _date_fact(db_session, sample_case)
    db_session.commit()
    event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", date_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    remove_timeline_event(db_session, event, actor="test-user")
    db_session.commit()
    remove_timeline_event(db_session, event, actor="test-user")
    db_session.commit()

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "timeline_event_deleted")
    ).all()
    assert len(entries) == 1


# --- list_timeline_events / compute_date_gaps -----------------------------


def test_list_timeline_events_excludes_deleted_and_sorts_chronologically(db_session: Session, sample_case: Case):
    later_fact = _date_fact(db_session, sample_case, fact_date=_APRIL_1)
    earlier_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_12)
    db_session.commit()

    later_event = create_timeline_event(
        db_session, sample_case, "meeting", "Later meeting", later_fact.fact_id, actor="test-user",
    )
    earlier_event = create_timeline_event(
        db_session, sample_case, "meeting", "Earlier meeting", earlier_fact.fact_id, actor="test-user",
    )
    deleted_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_20)
    db_session.commit()
    deleted_event = create_timeline_event(
        db_session, sample_case, "meeting", "Deleted meeting", deleted_fact.fact_id, actor="test-user",
    )
    db_session.commit()
    remove_timeline_event(db_session, deleted_event, actor="test-user")
    db_session.commit()

    results = list_timeline_events(db_session, sample_case.case_id)
    assert [e.event_id for e in results] == [earlier_event.event_id, later_event.event_id]


def test_list_timeline_events_filters_by_event_type(db_session: Session, sample_case: Case):
    meeting_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_12)
    eval_fact = _date_fact(db_session, sample_case, fact_date=_APRIL_1)
    db_session.commit()

    meeting_event = create_timeline_event(
        db_session, sample_case, "meeting", "A meeting", meeting_fact.fact_id, actor="test-user",
    )
    create_timeline_event(
        db_session, sample_case, "evaluation", "An evaluation", eval_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    results = list_timeline_events(db_session, sample_case.case_id, event_type_id=_event_type_id(db_session, "meeting"))
    assert [e.event_id for e in results] == [meeting_event.event_id]


def test_list_timeline_events_filters_by_date_range(db_session: Session, sample_case: Case):
    march_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_12)
    april_fact = _date_fact(db_session, sample_case, fact_date=_APRIL_1)
    db_session.commit()

    march_event = create_timeline_event(
        db_session, sample_case, "meeting", "March meeting", march_fact.fact_id, actor="test-user",
    )
    create_timeline_event(
        db_session, sample_case, "meeting", "April meeting", april_fact.fact_id, actor="test-user",
    )
    db_session.commit()

    results = list_timeline_events(
        db_session, sample_case.case_id, start_date=_MARCH_12, end_date=_MARCH_20,
    )
    assert [e.event_id for e in results] == [march_event.event_id]


def test_compute_date_gaps_first_is_none_and_gaps_are_correct(db_session: Session, sample_case: Case):
    march_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_12)
    later_fact = _date_fact(db_session, sample_case, fact_date=_MARCH_20)
    april_fact = _date_fact(db_session, sample_case, fact_date=_APRIL_1)
    db_session.commit()

    create_timeline_event(db_session, sample_case, "meeting", "First", march_fact.fact_id, actor="test-user")
    create_timeline_event(db_session, sample_case, "meeting", "Second", later_fact.fact_id, actor="test-user")
    create_timeline_event(db_session, sample_case, "meeting", "Third", april_fact.fact_id, actor="test-user")
    db_session.commit()

    events = list_timeline_events(db_session, sample_case.case_id)
    gaps = compute_date_gaps(events)

    assert gaps == [None, 8, 12]


def test_compute_date_gaps_empty_list():
    assert compute_date_gaps([]) == []
