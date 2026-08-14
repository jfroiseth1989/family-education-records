"""Core writes for the Fact, Observation & Summary Layer (Phase 3.5 Step 2).

See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "Fact, Observation &
Summary Layer". Four operations, matching the promotion workflow approved
in docs/PROJECT_PLAN.md Phase 3.5:

- `create_verified_fact()` -- a human asserts a fact directly, citing one
  or more existing `citations` rows. No AI involved.
- `create_ai_observation()` -- a machine-suggested candidate (Step 3's
  deterministic date-parser, or any future non-LLM heuristic) is
  recorded as `pending_review`. Never itself usable by any later phase.
- `promote_observation()` -- a human reviews a pending observation and
  accepts it, creating a *new* `verified_facts` row with lineage back to
  the observation (`source_observation_id`) and copying its citations.
  The observation row is never edited beyond its own review fields.
- `reject_observation()` -- a human reviews a pending observation and
  declines it. No fact is created.

Every fact-like record here carries a required citation to at least one
`citations` row (docs/ARCHITECTURE.md §3.7's "no bare, unattributed
fact" guarantee) and every citation must belong to the same case as the
fact/observation being created -- checked here, not just assumed. This
layer is genuinely case-scoped, not document-scoped (a fact can cite
citations spanning more than one document), so writes here are logged to
`audit_log` -- the case-level ledger -- rather than fabricated onto any
one cited document's `document_custody_events`; see
docs/DATA_MODEL.md "audit_log" and app/api/cases.py for the precedent.

Phase 4 Step 0 (docs/PHASE_4_IMPLEMENTATION_PLAN.md §1/§3) added
`fact_date`/`observed_date`: a structured date, required if and only if
`fact_type` is "date", so Phase 4's timeline can read a real date value
instead of a human re-typing what's already in a fact's `statement`
text.

Communications Phase Step 7 added the one exception to "every fact-like
record carries a required citation": a Communication-sourced observation
(`app/core/communications/timeline_suggestions.py::generate_timeline_suggestion()`)
is created directly, bypassing `create_ai_observation()`'s citation
requirement entirely, since a Communication is not a Document and has no
citable page/offset span -- its `AiObservation.communication_id` FK is
an equally direct provenance anchor, just a structurally different one.
`promote_observation()`/`reject_observation()` below are otherwise used
completely unmodified for that observation type (aside from
`promote_observation()` copying `communication_id` onto the resulting
`VerifiedFact`, a no-op for every Document-sourced observation, whose
`communication_id` is always null).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AiObservation,
    AiObservationCitation,
    AuditLog,
    Case,
    Citation,
    FactType,
    VerifiedFact,
    VerifiedFactCitation,
)

# certain / probable / uncertain -- the human's own certainty about a
# verified fact, required regardless of where the fact came from. See
# docs/DATA_MODEL.md "verified_facts" and docs/PROJECT_PLAN.md decision #10.
CONFIDENCE_LABELS = frozenset({"certain", "probable", "uncertain"})

# pending_review / accepted / rejected -- an ai_observations row's review
# lifecycle. Defined here (not just as a bare string in models.py) since
# this module is the only place that ever transitions status.
PENDING_REVIEW = "pending_review"
ACCEPTED = "accepted"
REJECTED = "rejected"


def _get_fact_type(db: Session, name: str) -> FactType:
    fact_type = db.scalars(select(FactType).where(FactType.name == name)).one_or_none()
    if fact_type is None:
        raise ValueError(f"Unknown fact type '{name}'.")
    return fact_type


def _validated_citations(db: Session, case: Case, citation_ids: list[int]) -> list[Citation]:
    """Fetch and validate citations for a new fact/observation.

    Raises ValueError if the list is empty (docs/ARCHITECTURE.md §3.7: no
    fact-like record without at least one citation) or if any citation
    doesn't exist or belongs to a document outside this case -- a case
    boundary a client-submitted citation_id list could otherwise violate.
    """
    if not citation_ids:
        raise ValueError("At least one citation is required.")

    citations = []
    for citation_id in citation_ids:
        citation = db.get(Citation, citation_id)
        if citation is None:
            raise ValueError(f"Citation {citation_id} not found.")
        if citation.document.case_id != case.case_id:
            raise ValueError(
                f"Citation {citation_id} belongs to a different case than this fact/observation."
            )
        citations.append(citation)
    return citations


def create_verified_fact(
    db: Session,
    case: Case,
    fact_type_name: str,
    statement: str,
    confidence_label: str,
    citation_ids: list[int],
    actor: str,
    fact_date: datetime | None = None,
) -> VerifiedFact:
    """A human directly asserts a fact, citing one or more existing citations.

    No AI observation involved -- `source_observation_id` and
    `confidence_score` stay null; only `promote_observation()` ever sets
    those. `fact_date` is required when `fact_type_name` is "date" (a
    date-type fact with no date defeats the entire point of Phase 4
    reading a structured date from here) and forbidden otherwise -- see
    docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 0. Raises ValueError for
    an empty statement, an invalid confidence label, a missing/
    unexpected `fact_date`, or invalid citations. Does not commit -- same
    convention as every other core module.
    """
    stripped = statement.strip()
    if not stripped:
        raise ValueError("Statement cannot be empty.")
    if confidence_label not in CONFIDENCE_LABELS:
        raise ValueError(
            f"Invalid confidence label '{confidence_label}'; must be one of {sorted(CONFIDENCE_LABELS)}."
        )
    if fact_type_name == "date" and fact_date is None:
        raise ValueError("fact_date is required when fact_type is 'date'.")
    if fact_type_name != "date" and fact_date is not None:
        raise ValueError(f"fact_date is only valid when fact_type is 'date', not '{fact_type_name}'.")
    fact_type = _get_fact_type(db, fact_type_name)
    citations = _validated_citations(db, case, citation_ids)

    fact = VerifiedFact(
        case_id=case.case_id,
        fact_type_id=fact_type.type_id,
        statement=stripped,
        confidence_label=confidence_label,
        fact_date=fact_date,
        created_by=actor,
    )
    db.add(fact)
    db.flush()  # assigns fact.fact_id

    for citation in citations:
        db.add(VerifiedFactCitation(fact_id=fact.fact_id, citation_id=citation.citation_id))

    db.add(
        AuditLog(
            case_id=case.case_id,
            event_type="verified_fact_created",
            entity_type="verified_fact",
            entity_id=fact.fact_id,
            actor=actor,
            details={"fact_type": fact_type_name, "citation_ids": citation_ids},
        )
    )
    return fact


def create_ai_observation(
    db: Session,
    case: Case,
    fact_type_name: str,
    statement: str,
    confidence_score: float,
    method: str,
    citation_ids: list[int],
    actor: str,
    observed_date: datetime | None = None,
) -> AiObservation:
    """Record a machine-suggested candidate fact as `pending_review`.

    Never usable by the timeline, conflict tracker, or binder narrative
    directly (docs/ARCHITECTURE.md §3.7) -- only `promote_observation()`
    can turn this into something later phases may read. `actor` is who/
    what produced this observation for the audit trail (e.g. "system
    (regex-date-parse-v1)") -- distinct from `method`, which is the
    ai_observations column identifying the specific technique.
    `observed_date` is the real date a date-type observation source
    (Step 3's date-parser) already computed internally -- same
    required-iff-date rule as `create_verified_fact()`'s `fact_date`, so
    a later promotion always has a structured date to carry forward. Raises
    ValueError for an empty statement, an out-of-range confidence score,
    an empty method, a missing/unexpected `observed_date`, or invalid
    citations. Does not commit.
    """
    stripped = statement.strip()
    if not stripped:
        raise ValueError("Statement cannot be empty.")
    if not (0.0 <= confidence_score <= 1.0):
        raise ValueError(f"Confidence score {confidence_score} must be between 0.0 and 1.0.")
    stripped_method = method.strip()
    if not stripped_method:
        raise ValueError("Method cannot be empty.")
    if fact_type_name == "date" and observed_date is None:
        raise ValueError("observed_date is required when fact_type is 'date'.")
    if fact_type_name != "date" and observed_date is not None:
        raise ValueError(f"observed_date is only valid when fact_type is 'date', not '{fact_type_name}'.")
    fact_type = _get_fact_type(db, fact_type_name)
    citations = _validated_citations(db, case, citation_ids)

    observation = AiObservation(
        case_id=case.case_id,
        fact_type_id=fact_type.type_id,
        statement=stripped,
        confidence_score=confidence_score,
        method=stripped_method,
        observed_date=observed_date,
        status=PENDING_REVIEW,
    )
    db.add(observation)
    db.flush()  # assigns observation.observation_id

    for citation in citations:
        db.add(
            AiObservationCitation(observation_id=observation.observation_id, citation_id=citation.citation_id)
        )

    db.add(
        AuditLog(
            case_id=case.case_id,
            event_type="ai_observation_created",
            entity_type="ai_observation",
            entity_id=observation.observation_id,
            actor=actor,
            details={"fact_type": fact_type_name, "method": stripped_method, "citation_ids": citation_ids},
        )
    )
    return observation


def promote_observation(
    db: Session,
    observation: AiObservation,
    confidence_label: str,
    actor: str,
    statement: str | None = None,
    fact_date: datetime | None = None,
) -> VerifiedFact:
    """A human reviews a pending observation and accepts it.

    Creates a *new* `verified_facts` row with `source_observation_id`
    lineage back to `observation` and the same citations -- the
    observation row itself is never edited beyond its own `status`/
    `reviewed_by`/`reviewed_at`. `confidence_label` is required and
    always supplied by the human reviewing, never derived from
    `observation.confidence_score` -- the two are different concepts
    (docs/DATA_MODEL.md "verified_facts"). `statement` optionally lets
    the reviewer correct the wording before confirming; defaults to the
    observation's own statement unchanged. `fact_date` works the same way
    for the underlying date -- defaults to `observation.observed_date`,
    overridable if the reviewer needs to correct it (docs/PHASE_4_
    IMPLEMENTATION_PLAN.md §3 Step 0). `communication_id` is copied
    unchanged from `observation` onto the new fact (Communications Phase
    Step 7) -- always null for a Document-sourced observation, so this
    is a no-op for every pre-existing caller. Raises ValueError if
    `observation` isn't `pending_review` -- there is no re-promote or
    re-reject path. Does not commit.
    """
    if observation.status != PENDING_REVIEW:
        raise ValueError(
            f"Observation {observation.observation_id} is already '{observation.status}', not pending review."
        )
    if confidence_label not in CONFIDENCE_LABELS:
        raise ValueError(
            f"Invalid confidence label '{confidence_label}'; must be one of {sorted(CONFIDENCE_LABELS)}."
        )

    final_statement = (statement.strip() if statement is not None else observation.statement)
    if not final_statement:
        raise ValueError("Statement cannot be empty.")

    final_fact_date = fact_date if fact_date is not None else observation.observed_date
    if observation.fact_type.name == "date" and final_fact_date is None:
        raise ValueError("fact_date is required when promoting a 'date' observation.")
    if observation.fact_type.name != "date" and final_fact_date is not None:
        raise ValueError(f"fact_date is only valid for 'date' observations, not '{observation.fact_type.name}'.")

    fact = VerifiedFact(
        case_id=observation.case_id,
        fact_type_id=observation.fact_type_id,
        statement=final_statement,
        confidence_label=confidence_label,
        confidence_score=observation.confidence_score,
        fact_date=final_fact_date,
        source_observation_id=observation.observation_id,
        # Communications Phase Step 7: direct traceability back to the
        # source email, mirrored from the observation. Always None for
        # every Document-sourced observation (no behavior change there).
        communication_id=observation.communication_id,
        created_by=actor,
    )
    db.add(fact)
    db.flush()  # assigns fact.fact_id

    observation_citation_ids = db.scalars(
        select(AiObservationCitation.citation_id).where(
            AiObservationCitation.observation_id == observation.observation_id
        )
    ).all()
    for citation_id in observation_citation_ids:
        db.add(VerifiedFactCitation(fact_id=fact.fact_id, citation_id=citation_id))

    now = datetime.now(timezone.utc)
    observation.status = ACCEPTED
    observation.reviewed_by = actor
    observation.reviewed_at = now

    db.add(
        AuditLog(
            case_id=observation.case_id,
            event_type="ai_observation_promoted",
            entity_type="ai_observation",
            entity_id=observation.observation_id,
            actor=actor,
            details={"promoted_to_fact_id": fact.fact_id},
        )
    )
    db.add(
        AuditLog(
            case_id=observation.case_id,
            event_type="verified_fact_created",
            entity_type="verified_fact",
            entity_id=fact.fact_id,
            actor=actor,
            details={"source_observation_id": observation.observation_id},
        )
    )
    return fact


def reject_observation(db: Session, observation: AiObservation, actor: str, reason: str | None = None) -> AiObservation:
    """A human reviews a pending observation and declines it.

    No `verified_facts` row is created. The observation row is retained
    exactly as generated -- only `status`/`reviewed_by`/`reviewed_at`
    change -- so the suggestion-to-decision history stays auditable even
    for a rejected suggestion. Raises ValueError if `observation` isn't
    `pending_review`. Does not commit.
    """
    if observation.status != PENDING_REVIEW:
        raise ValueError(
            f"Observation {observation.observation_id} is already '{observation.status}', not pending review."
        )

    now = datetime.now(timezone.utc)
    observation.status = REJECTED
    observation.reviewed_by = actor
    observation.reviewed_at = now

    db.add(
        AuditLog(
            case_id=observation.case_id,
            event_type="ai_observation_rejected",
            entity_type="ai_observation",
            entity_id=observation.observation_id,
            actor=actor,
            details={"reason": reason} if reason else None,
        )
    )
    return observation


def list_pending_observations(db: Session, case_id: int) -> list[AiObservation]:
    """Pending-review observations for a case, oldest first (review-queue order)."""
    return db.scalars(
        select(AiObservation)
        .where(AiObservation.case_id == case_id, AiObservation.status == PENDING_REVIEW)
        .order_by(AiObservation.created_at)
    ).all()


def list_verified_facts(db: Session, case_id: int) -> list[VerifiedFact]:
    """Non-deleted verified facts for a case, most recent first."""
    return db.scalars(
        select(VerifiedFact)
        .where(VerifiedFact.case_id == case_id, VerifiedFact.deleted_at.is_(None))
        .order_by(VerifiedFact.created_at.desc())
    ).all()


def list_fact_types(db: Session) -> list[FactType]:
    """Active fact types, for populating a fact-type selector in the UI."""
    return db.scalars(select(FactType).where(FactType.is_active.is_(True)).order_by(FactType.name)).all()
