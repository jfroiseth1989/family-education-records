"""Idempotent flag creation and review-lifecycle transitions for IEP
Consistency Review (Step 3).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §7. `scan_document_for_service_inconsistencies()`
runs app/core/iep_consistency/rules.py's pure comparison over one
document's active service records and turns each `FlagCandidate` into
an `iep_inconsistency_flags` row -- but only if no flag with the same
`dedup_key` already exists for this case, in *any* status. That's the
whole idempotency mechanism: a `pending` flag is never duplicated, and
a `confirmed`/`dismissed` flag is never resurrected as a fresh
`pending` one, because re-running the scan against unchanged
`iep_records` always recomputes the identical `dedup_key`.

Never writes to `iep_records`/`iep_record_fields` -- comparison is
strictly read-only over the structured data Step 2 produced. Never
sets `linked_timeline_event_id`/`linked_verified_fact_id` -- attaching
a flag to a timeline event or verified fact is a distinct, explicit
human action (not built in this step; the columns exist on the schema
from Step 1 for a later step to use), never something a scan or a
confirm/dismiss transition does on its own.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.iep_consistency.rules import compare_service_records_within_document
from app.core.iep_extraction.lookups import get_inconsistency_type
from app.db.models import Case, Document, IepInconsistencyFlag, IepRecord, IepRecordType

METHOD = "iep-service-comparison-v1"


def _compute_dedup_key(
    inconsistency_type_id: int,
    source_a_record_id: int,
    source_a_field_id: int | None,
    source_b_record_id: int,
    source_b_field_id: int | None,
    rule_id: str,
) -> str:
    """A deterministic hash of the comparison's identity -- which
    rule, which two records/fields (docs/IEP_CONSISTENCY_REVIEW_PLAN.md
    §2.4). Does not include `case_id`: the schema's uniqueness
    constraint is `(case_id, dedup_key)` together, exactly as the plan
    specifies, so the same comparison identity hashed here is scoped
    to a case by the database constraint, not by this function.
    """
    parts = "|".join(
        str(part)
        for part in (
            inconsistency_type_id,
            source_a_record_id,
            source_a_field_id,
            source_b_record_id,
            source_b_field_id,
            rule_id,
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def scan_document_for_service_inconsistencies(
    db: Session, case: Case, document: Document, actor: str
) -> list[IepInconsistencyFlag]:
    """Compare this document's active service-type `iep_records`
    against each other and create `iep_inconsistency_flags` rows for
    any deterministic mismatch (§4 rules #1-#2, scoped to service
    records -- the only structured type Step 2 built).

    `actor` is accepted for signature symmetry with the rest of this
    application's scan/extract functions (see
    app/core/iep_extraction/services.py::extract_service_records) but
    isn't stored on the flag itself -- a flag has no "created_by" field
    in the schema; provenance is instead the deterministic rule
    (`rule_id`) plus the two source records, which is what makes it
    reproducible in the first place. Returns only newly created flags
    (empty if every candidate comparison already has a flag). Does not
    commit -- same convention as every other core module in this
    application.
    """
    del actor  # accepted for signature symmetry; nothing to attribute a deterministic rule to
    records = list(
        db.scalars(
            select(IepRecord)
            .join(IepRecordType, IepRecordType.type_id == IepRecord.record_type_id)
            .where(
                IepRecord.document_id == document.document_id,
                IepRecordType.name == "service",
                IepRecord.status == "active",
            )
        ).all()
    )
    candidates = compare_service_records_within_document(records)

    created: list[IepInconsistencyFlag] = []
    for candidate in candidates:
        inconsistency_type = get_inconsistency_type(db, candidate.inconsistency_type_name)
        dedup_key = _compute_dedup_key(
            inconsistency_type.type_id,
            candidate.source_a_record_id,
            candidate.source_a_field_id,
            candidate.source_b_record_id,
            candidate.source_b_field_id,
            candidate.rule_id,
        )

        already_exists = (
            db.scalars(
                select(IepInconsistencyFlag.flag_id).where(
                    IepInconsistencyFlag.case_id == case.case_id,
                    IepInconsistencyFlag.dedup_key == dedup_key,
                )
            ).first()
            is not None
        )
        if already_exists:
            continue

        flag = IepInconsistencyFlag(
            case_id=case.case_id,
            inconsistency_type_id=inconsistency_type.type_id,
            comparison_mode=candidate.comparison_mode,
            source_a_record_id=candidate.source_a_record_id,
            source_a_field_id=candidate.source_a_field_id,
            source_b_record_id=candidate.source_b_record_id,
            source_b_field_id=candidate.source_b_field_id,
            rule_id=candidate.rule_id,
            reason_text=candidate.reason_text,
            extracted_value_a=candidate.extracted_value_a,
            extracted_value_b=candidate.extracted_value_b,
            dedup_key=dedup_key,
        )
        db.add(flag)
        db.flush()  # assigns flag.flag_id
        created.append(flag)

    return created


def list_inconsistency_flags(db: Session, case_id: int) -> list[IepInconsistencyFlag]:
    """Every flag for this case, most recently created first. Callers
    that want to group by status (see app/api/iep_consistency.py's
    Consistency Review page) filter this list themselves -- kept as
    one simple, read-only query rather than three, so "all flags for
    this case" stays the single source of truth.
    """
    return list(
        db.scalars(
            select(IepInconsistencyFlag)
            .where(IepInconsistencyFlag.case_id == case_id)
            .order_by(IepInconsistencyFlag.created_at.desc())
        ).all()
    )


def confirm_flag(flag: IepInconsistencyFlag, actor: str) -> None:
    """A human reviewed both sources and agrees they're inconsistent.

    Never means "FERChronos asserts this is a real problem" -- only
    that a human looked and agreed (§9). Reversible: calling
    `dismiss_flag()` afterward simply moves it to `dismissed`: neither
    transition is a one-way door. Does not commit.
    """
    flag.status = "confirmed"
    flag.reviewed_by = actor
    flag.reviewed_at = datetime.now(timezone.utc)


def dismiss_flag(flag: IepInconsistencyFlag, actor: str) -> None:
    """A human reviewed both sources and decided this isn't a real
    inconsistency (e.g. an intentional, documented change) -- the
    "Mark not an inconsistency" mockup action. The comparison rule
    itself never suppresses this case at generation time (§4 rule #7's
    discussion); only a human deciding here does. Does not commit.
    """
    flag.status = "dismissed"
    flag.reviewed_by = actor
    flag.reviewed_at = datetime.now(timezone.utc)


def set_flag_note(flag: IepInconsistencyFlag, note_text: str | None) -> None:
    """Attach or clear the human's own free-text note -- the only
    free-text field anywhere in this schema (§9); never generated or
    modified by any rule. Does not change `status`. Does not commit.
    """
    flag.user_note = note_text
