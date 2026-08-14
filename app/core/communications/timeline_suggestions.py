"""Deterministic timeline suggestions from imported Communications
(Communications Phase Step 7).

A `Communication` never automatically becomes a `VerifiedFact` or
`TimelineEvent` -- this module only ever creates an `AiObservation`
(`status="pending_review"`), the exact same suggestion/candidate concept
`app/core/facts/date_extraction.py` already uses for Document text.
Reuses that module's review lifecycle end to end rather than building a
parallel one: `app/core/facts/service.py::promote_observation()`/
`reject_observation()` are called completely unmodified (aside from one
additive line in `promote_observation()` itself -- see that function's
docstring -- copying `communication_id` onto the resulting `VerifiedFact`
when present) to Approve/Edit+Approve/Reject a Communication-sourced
observation exactly as they already do for a Document-sourced one; the
existing `/cases/{case_id}/facts` review queue already lists both kinds
side by side, since `list_pending_observations()` doesn't filter by
source.

`fact_type_name` is deliberately the existing "date" type, not a new
"communication" type -- reusing it is what makes a promoted suggestion
immediately eligible to anchor a real `TimelineEvent` through the
existing, completely unmodified
`app/core/timeline/service.py::list_date_source_candidates()`/
`create_timeline_event()` (both filter/require `fact_type='date'` with a
non-null `fact_date`). Nothing in this module or the timeline layer
needs to change for a Communication-sourced fact to become a timeline
event through the exact same human-gated "pick a date-source fact, give
it a title and event type" step already used for every other fact.

Candidate text is always exactly:
    Email received from <sender> regarding "<subject>" on <date>.
built only from `Communication.from_display_name`/`from_address`,
`subject`, and its own `sent_at`/`received_at` -- no body text, no
inference, no generative summarization, no network call. `received_at`
is preferred over `sent_at` when both are known (mirrors
`app/core/communications/attachment_metadata.py::suggest_date_received`'s
same reasoning -- either is direct evidence of when the family had this
message, `received_at` more so); which one was actually used is recorded
in the observation's audit-log details as `date_basis` (`"sent"` or
`"received"`) so that distinction is never silently lost, even though
the candidate sentence's wording doesn't change between the two.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.attachment_metadata import _sender_label
from app.core.facts.service import PENDING_REVIEW, _get_fact_type
from app.db.models import AiObservation, AuditLog, Communication

SYSTEM_ACTOR = "system (communication-timeline-suggestion-v1)"
METHOD = "communication-timeline-suggestion-v1"
FACT_TYPE_NAME = "date"


def _resolve_date(communication: Communication) -> tuple[datetime | None, str | None]:
    """The best reliable date already stored on `communication`, and
    which field it came from -- `received_at` preferred, `sent_at` as
    fallback, `(None, None)` if neither is known. Never guesses or
    derives a date from anything else (e.g. never `imported_at`, which
    only reflects when this application happened to see the message).
    """
    if communication.received_at is not None:
        return communication.received_at, "received"
    if communication.sent_at is not None:
        return communication.sent_at, "sent"
    return None, None


def compose_candidate_statement(communication: Communication) -> tuple[str, datetime, str] | None:
    """The deterministic candidate sentence, its date, and the date's
    basis ("sent" or "received") -- or None if there's no reliable date
    to build it from at all (the one hard requirement; see module
    docstring). Pure function of already-parsed Communication fields --
    no DB access, no side effects.
    """
    moment, basis = _resolve_date(communication)
    if moment is None:
        return None

    sender = _sender_label(communication)
    subject = communication.subject or "(no subject)"
    statement = f'Email received from {sender} regarding "{subject}" on {moment.date().isoformat()}.'
    return statement, moment, basis


def generate_timeline_suggestion(
    db: Session, communication: Communication, actor: str = SYSTEM_ACTOR
) -> AiObservation | None:
    """Create a pending-review timeline suggestion for `communication`, or
    return None if one shouldn't be created.

    None covers every case the caller doesn't need to distinguish: the
    communication is soft-deleted, has no assigned student yet
    (`case_id` is required on `ai_observations`), has no reliable
    sent/received date, or already has a suggestion (regardless of its
    current status -- pending, accepted, or rejected). Re-running this
    for the same communication is always safe: at most one
    `AiObservation` row will ever exist for it, a DB-enforced invariant
    (`AiObservation.communication_id` is UNIQUE) backing the check here.
    Does not commit; the caller controls the transaction boundary, same
    convention as every other core module.
    """
    if communication.deleted_at is not None:
        return None
    if communication.case_id is None:
        return None

    existing = db.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).first()
    if existing is not None:
        return None

    candidate = compose_candidate_statement(communication)
    if candidate is None:
        return None
    statement, moment, basis = candidate

    fact_type = _get_fact_type(db, FACT_TYPE_NAME)
    observation = AiObservation(
        case_id=communication.case_id,
        fact_type_id=fact_type.type_id,
        statement=statement,
        confidence_score=1.0,
        method=METHOD,
        observed_date=moment,
        communication_id=communication.communication_id,
        status=PENDING_REVIEW,
    )
    db.add(observation)
    db.flush()  # assigns observation.observation_id

    db.add(
        AuditLog(
            case_id=communication.case_id,
            event_type="ai_observation_created",
            entity_type="ai_observation",
            entity_id=observation.observation_id,
            actor=actor,
            details={
                "fact_type": FACT_TYPE_NAME,
                "method": METHOD,
                "communication_id": communication.communication_id,
                "date_basis": basis,
            },
        )
    )
    return observation
