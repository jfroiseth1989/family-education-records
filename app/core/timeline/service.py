"""Core writes for the timeline (Phase 4 Step 2).

See docs/ARCHITECTURE.md §3.8 and docs/PHASE_4_IMPLEMENTATION_PLAN.md
§2/§4. Every timeline event has exactly one **date-source fact** -- a
non-deleted `verified_facts` row with `fact_type='date'` and a non-null
`fact_date` -- plus zero or more supporting facts of any type, all
belonging to the same case. `event_date` is copied from the date-source
fact at creation and never changes afterward -- there is no re-anchor
path in v1, only removal (soft-delete) and recreation if a correction is
needed. Nothing here ever reads `document_pages`/`citations`/
`ai_observations` directly -- only through an already-human-confirmed
`verified_facts` row, exactly as the standing rule requires.

`create_timeline_event()` -- creates the event plus its date-source and
any initial supporting facts, in one call.
`attach_fact_to_event()` -- adds one more supporting fact later
(incremental attachment, docs/PHASE_4_IMPLEMENTATION_PLAN.md §1
decision 4). Never attaches a second date-source -- `is_date_source` is
always False here.
`remove_timeline_event()` -- soft-delete only (`deleted_at`); no
hard-delete path anywhere in this application.
`list_timeline_events()` / `compute_date_gaps()` -- read helpers for
Step 3's timeline view.

This layer is case-scoped, not document-scoped, so writes are logged to
`audit_log` -- the case-level ledger -- same precedent as
app/core/facts/service.py and app/api/cases.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Case, EventType, TimelineEvent, TimelineEventFact, VerifiedFact

# exact / approximate / range -- same three values as
# DocumentDatePrecision, but kept as a local frozenset (not a shared
# enum) since this module's callers pass plain strings, matching every
# other core module's convention (see app/core/facts/service.py's
# CONFIDENCE_LABELS).
EVENT_DATE_PRECISIONS = frozenset({"exact", "approximate", "range"})

# Always this value in v1 -- no system-suggested-event workflow exists
# (docs/PHASE_4_IMPLEMENTATION_PLAN.md §1 decision 2).
EVENT_DATE_SOURCE = "verified_fact"
CREATED_BY = "manual"
STATUS_CONFIRMED = "confirmed"


def _get_event_type(db: Session, name: str) -> EventType:
    event_type = db.scalars(select(EventType).where(EventType.name == name)).one_or_none()
    if event_type is None:
        raise ValueError(f"Unknown event type '{name}'.")
    return event_type


def _get_case_fact(db: Session, case: Case, fact_id: int) -> VerifiedFact:
    """Fetch a non-deleted verified fact and confirm it belongs to `case`.

    Raises ValueError otherwise. Does not check fact_type -- callers that
    need a date-source fact specifically use `_get_date_source_fact()`.
    """
    fact = db.get(VerifiedFact, fact_id)
    if fact is None or fact.deleted_at is not None:
        raise ValueError(f"Verified fact {fact_id} not found.")
    if fact.case_id != case.case_id:
        raise ValueError(f"Verified fact {fact_id} belongs to a different case.")
    return fact


def _get_date_source_fact(db: Session, case: Case, fact_id: int) -> VerifiedFact:
    """Fetch a verified fact suitable for anchoring a timeline event.

    Raises ValueError if it doesn't exist, is soft-deleted, belongs to a
    different case, or isn't a `fact_type='date'` fact with a non-null
    `fact_date` -- see docs/PHASE_4_IMPLEMENTATION_PLAN.md §2.
    """
    fact = _get_case_fact(db, case, fact_id)
    if fact.fact_type.name != "date" or fact.fact_date is None:
        raise ValueError(
            f"Verified fact {fact_id} is not a 'date' fact with a fact_date; "
            "it cannot anchor a timeline event."
        )
    return fact


def _validate_precision(precision: str, range_end: datetime | None, event_date: datetime) -> None:
    if precision not in EVENT_DATE_PRECISIONS:
        raise ValueError(
            f"Invalid event_date_precision '{precision}'; must be one of {sorted(EVENT_DATE_PRECISIONS)}."
        )
    if precision == "range":
        if range_end is None:
            raise ValueError("event_date_range_end is required when event_date_precision is 'range'.")
        # SQLite doesn't reliably round-trip tzinfo on DateTime(timezone=True)
        # columns -- a value just read back from the DB in a fresh session
        # (like `event_date` almost always is here) comes back naive, while
        # a freshly-constructed value (like a route's just-parsed form
        # input) stays timezone-aware. Comparing them directly raises
        # TypeError. Every DateTime(timezone=True) column in this app is
        # effectively naive-UTC on disk regardless of what Python object
        # wrote it, so stripping tzinfo from both sides before comparing is
        # the correct normalization, not a workaround.
        if range_end.replace(tzinfo=None) < event_date.replace(tzinfo=None):
            raise ValueError("event_date_range_end must be on or after the date-source fact's date.")
    elif range_end is not None:
        raise ValueError(
            f"event_date_range_end was given but event_date_precision is '{precision}', not 'range'."
        )


def create_timeline_event(
    db: Session,
    case: Case,
    event_type_name: str,
    title: str,
    date_fact_id: int,
    actor: str,
    description: str | None = None,
    additional_fact_ids: list[int] | None = None,
    event_date_precision: str = "exact",
    event_date_range_end: datetime | None = None,
) -> TimelineEvent:
    """Create a timeline event anchored to one verified date-type fact.

    `date_fact_id` must reference a non-deleted `verified_facts` row in
    this case with `fact_type='date'` and a non-null `fact_date` -- that
    fact's `fact_date` becomes `event_date`, copied once, never updated
    afterward. `additional_fact_ids` (optional) attaches supporting
    facts of any type from the same case, deduplicated against
    `date_fact_id` and against each other. Raises ValueError for an
    empty title, an invalid date-source fact, an invalid supporting
    fact, or an invalid precision/range_end combination. Does not commit
    -- same convention as every other core module.
    """
    stripped_title = title.strip()
    if not stripped_title:
        raise ValueError("Title cannot be empty.")
    final_description = (description or "").strip() or None

    event_type = _get_event_type(db, event_type_name)
    date_fact = _get_date_source_fact(db, case, date_fact_id)
    _validate_precision(event_date_precision, event_date_range_end, date_fact.fact_date)

    unique_additional_ids = [
        fact_id for fact_id in dict.fromkeys(additional_fact_ids or []) if fact_id != date_fact_id
    ]
    supporting_facts = [_get_case_fact(db, case, fact_id) for fact_id in unique_additional_ids]

    event = TimelineEvent(
        case_id=case.case_id,
        event_date=date_fact.fact_date,
        event_date_range_end=event_date_range_end,
        event_date_precision=event_date_precision,
        event_date_source=EVENT_DATE_SOURCE,
        title=stripped_title,
        description=final_description,
        event_type_id=event_type.type_id,
        created_by=CREATED_BY,
        status=STATUS_CONFIRMED,
    )
    db.add(event)
    db.flush()  # assigns event.event_id

    db.add(TimelineEventFact(event_id=event.event_id, fact_id=date_fact.fact_id, is_date_source=True))
    for fact in supporting_facts:
        db.add(TimelineEventFact(event_id=event.event_id, fact_id=fact.fact_id, is_date_source=False))

    db.add(
        AuditLog(
            case_id=case.case_id,
            event_type="timeline_event_created",
            entity_type="timeline_event",
            entity_id=event.event_id,
            actor=actor,
            details={
                "event_type": event_type_name,
                "date_fact_id": date_fact_id,
                "additional_fact_ids": unique_additional_ids,
            },
        )
    )
    return event


def attach_fact_to_event(db: Session, event: TimelineEvent, fact_id: int, actor: str) -> TimelineEventFact:
    """Attach one more supporting fact to an existing event.

    Never attaches a second date-source (`is_date_source` is always
    False here) -- an event's date is fixed at creation, see the module
    docstring. Raises ValueError if the event is soft-deleted, the fact
    doesn't belong to the event's case, or the fact is already attached.
    Does not commit.
    """
    if event.deleted_at is not None:
        raise ValueError(f"Timeline event {event.event_id} has been deleted.")

    case = db.get(Case, event.case_id)
    fact = _get_case_fact(db, case, fact_id)

    existing = db.get(TimelineEventFact, (event.event_id, fact.fact_id))
    if existing is not None:
        raise ValueError(f"Fact {fact_id} is already attached to this event.")

    link = TimelineEventFact(event_id=event.event_id, fact_id=fact.fact_id, is_date_source=False)
    db.add(link)

    db.add(
        AuditLog(
            case_id=event.case_id,
            event_type="timeline_event_fact_attached",
            entity_type="timeline_event",
            entity_id=event.event_id,
            actor=actor,
            details={"fact_id": fact_id},
        )
    )
    return link


def remove_timeline_event(db: Session, event: TimelineEvent, actor: str) -> None:
    """Soft-delete a timeline event (sets `deleted_at`).

    A no-op if already removed. Never touches the event's linked
    `verified_facts` rows or their citations -- those remain permanent
    traceability records, same as every other soft-delete in this
    application (docs/DATA_MODEL.md design principle #3).
    """
    if event.deleted_at is not None:
        return

    event.deleted_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            case_id=event.case_id,
            event_type="timeline_event_deleted",
            entity_type="timeline_event",
            entity_id=event.event_id,
            actor=actor,
        )
    )


def list_timeline_events(
    db: Session,
    case_id: int,
    event_type_id: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[TimelineEvent]:
    """Non-deleted timeline events for a case, chronological order.

    `event_type_id`/`start_date`/`end_date` are optional filters for
    Step 3's timeline view.
    """
    query = select(TimelineEvent).where(
        TimelineEvent.case_id == case_id, TimelineEvent.deleted_at.is_(None)
    )
    if event_type_id is not None:
        query = query.where(TimelineEvent.event_type_id == event_type_id)
    if start_date is not None:
        query = query.where(TimelineEvent.event_date >= start_date)
    if end_date is not None:
        query = query.where(TimelineEvent.event_date <= end_date)

    return db.scalars(query.order_by(TimelineEvent.event_date)).all()


def compute_date_gaps(events: list[TimelineEvent]) -> list[int | None]:
    """Day-count gap before each event, for a chronologically sorted list.

    The first event's gap is always None (nothing precedes it). No
    threshold or "is this significant" judgment is made here -- the
    caller (Step 3's UI) shows the plain number and lets a human decide
    what matters for their case, consistent with this app's
    surface-don't-judge stance elsewhere (docs/PRIVACY_SECURITY.md §9).
    Assumes `events` is already sorted by `event_date` (as
    `list_timeline_events()` returns it) -- does not re-sort.
    """
    gaps: list[int | None] = []
    previous_date = None
    for event in events:
        if previous_date is None:
            gaps.append(None)
        else:
            gaps.append((event.event_date.date() - previous_date.date()).days)
        previous_date = event.event_date
    return gaps


def list_event_types(db: Session) -> list[EventType]:
    """Active event types, for populating an event-type selector in the UI."""
    return db.scalars(select(EventType).where(EventType.is_active.is_(True)).order_by(EventType.name)).all()


def list_date_source_candidates(db: Session, case_id: int) -> list[VerifiedFact]:
    """Non-deleted `fact_type='date'` facts with a `fact_date`, for a case.

    Exactly the set of facts eligible to anchor a new timeline event --
    see `_get_date_source_fact()`'s validation, which this listing
    mirrors so nothing offered in the UI's date-source selector could
    ever fail that check.
    """
    return db.scalars(
        select(VerifiedFact)
        .where(
            VerifiedFact.case_id == case_id,
            VerifiedFact.deleted_at.is_(None),
            VerifiedFact.fact_date.is_not(None),
        )
        .order_by(VerifiedFact.fact_date)
    ).all()


def get_event_facts(db: Session, event_id: int) -> tuple[VerifiedFact | None, list[VerifiedFact]]:
    """The (date-source fact, [supporting facts]) for one timeline event.

    date-source is None only if the event's own data is somehow
    inconsistent (should never happen given create_timeline_event()'s
    invariant) -- callers should treat None defensively, not assume it.
    """
    links = db.scalars(
        select(TimelineEventFact).where(TimelineEventFact.event_id == event_id)
    ).all()
    date_source = None
    supporting: list[VerifiedFact] = []
    for link in links:
        fact = db.get(VerifiedFact, link.fact_id)
        if link.is_date_source:
            date_source = fact
        else:
            supporting.append(fact)
    return date_source, supporting
