"""Bulk orchestration over the Step 5 single-attachment review logic
(Communications Phase Step 11).

This module is deliberately thin: it never classifies, never computes
metadata suggestions, never touches the vault, and never decides
duplicate-vs-new-Document on its own. Every one of those decisions
still belongs to `app/core/communications/attachment_classification.py`,
`attachment_metadata.py`, and (most importantly) `promotion.py`'s
`promote_attachment_to_document()`/`exclude_attachment()`/
`leave_attachment_with_email()`, called here unchanged, one attachment
at a time. What this module adds is exactly two things Step 5 never
needed at single-attachment scale: a filterable listing of attachments
still awaiting review, and a way to run the *same* per-item functions
across many attachments with per-item isolation (one failure never
undoes another attachment's success) and a plain-English result summary.

`_existing_document_for()` is the one piece of read-only, side-effect-
free logic this module owns itself: mirrors the exact `(case_id,
sha256_hash)` duplicate rule `app/core/ingestion/service.py::ingest_document()`
already enforces, so a listing/preview can say "this will link an
existing Document" *before* anything is committed -- without ever
calling the real ingestion path just to find out and then discarding
the result (which would be unsafe: `ingest_document()` copies bytes
into the vault as a side effect that a database rollback cannot undo).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.promotion import (
    AttachmentNotPromotableError,
    PromotionResult,
    exclude_attachment,
    leave_attachment_with_email,
    promote_attachment_to_document,
)
from app.core.ingestion.service import DuplicateDocumentError
from app.core.vault import VaultLayout
from app.db.models import (
    Communication,
    CommunicationAttachment,
    CommunicationImportBatchItem,
    Document,
)

# --- listing / filtering -----------------------------------------------


def _existing_document_for(db: Session, attachment: CommunicationAttachment) -> Document | None:
    """The Document `attachment` would link to (never create a new one
    for) if promoted right now -- the same `(case_id, sha256_hash)` rule
    `ingest_document()` itself uses. Read-only: never writes anything.
    """
    case = attachment.communication.case
    if case is None:
        return None
    return db.scalars(
        select(Document).where(Document.case_id == case.case_id, Document.sha256_hash == attachment.sha256_hash)
    ).first()


@dataclass(frozen=True)
class AttachmentReviewFilters:
    batch_id: int | None = None
    case_id: int | None = None
    document_type_id: int | None = None
    review_status: str = "pending"  # "pending" / "added_to_documents" / "excluded" / "left_with_email" / "" (any)
    candidate_filter: str = ""  # "" / "recognized" / "unrecognized" / "duplicates"
    sender: str = ""
    subject: str = ""
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True)
class AttachmentReviewRow:
    attachment: CommunicationAttachment
    existing_document: Document | None


def list_review_attachments(db: Session, filters: AttachmentReviewFilters) -> list[AttachmentReviewRow]:
    """Attachments matching `filters`, each paired with the Document it
    would link to if promoted now (or `None` if it would create a new
    one, or if promotion isn't currently possible for it). Newest
    Communication first, matching every other Communications list in
    this application.
    """
    query = select(CommunicationAttachment).join(
        Communication, CommunicationAttachment.communication_id == Communication.communication_id
    )

    if filters.batch_id is not None:
        query = query.join(
            CommunicationImportBatchItem,
            CommunicationImportBatchItem.communication_id == Communication.communication_id,
        ).where(CommunicationImportBatchItem.batch_id == filters.batch_id)
    if filters.case_id is not None:
        query = query.where(Communication.case_id == filters.case_id)
    if filters.document_type_id is not None:
        query = query.where(CommunicationAttachment.suggested_document_type_id == filters.document_type_id)
    if filters.review_status:
        query = query.where(CommunicationAttachment.review_status == filters.review_status)
    if filters.candidate_filter == "recognized":
        query = query.where(CommunicationAttachment.is_educational_record_candidate.is_(True))
    elif filters.candidate_filter == "unrecognized":
        query = query.where(CommunicationAttachment.is_educational_record_candidate.is_(False))
    if filters.sender.strip():
        pattern = f"%{filters.sender.strip()}%"
        query = query.where(
            (Communication.from_address.ilike(pattern)) | (Communication.from_display_name.ilike(pattern))
        )
    if filters.subject.strip():
        query = query.where(Communication.subject.ilike(f"%{filters.subject.strip()}%"))
    if filters.date_from is not None:
        query = query.where(Communication.sent_at >= filters.date_from)
    if filters.date_to is not None:
        query = query.where(Communication.sent_at < filters.date_to)

    query = query.order_by(Communication.sent_at.desc().nullslast(), CommunicationAttachment.attachment_id)

    attachments = list(db.scalars(query.distinct()).all())
    rows = [AttachmentReviewRow(attachment=a, existing_document=_existing_document_for(db, a)) for a in attachments]

    if filters.candidate_filter == "duplicates":
        rows = [r for r in rows if r.existing_document is not None]

    return rows


def list_recognized_pending_attachments(
    db: Session, *, batch_id: int | None = None, case_id: int | None = None
) -> list[CommunicationAttachment]:
    """The exact "Add all recognized" candidate set: `pending` review
    status and an unambiguous classifier suggestion. Deliberately never
    includes an unsupported/unreadable/ambiguous file -- all three
    collapse to `is_educational_record_candidate = False` already (see
    `attachment_classification.py::classify_attachment()`'s docstring),
    so this filter alone is the whole safety guarantee "Add all
    recognized" requires; nothing else needs to check for those cases
    separately.
    """
    filters = AttachmentReviewFilters(
        batch_id=batch_id, case_id=case_id, review_status="pending", candidate_filter="recognized"
    )
    return [row.attachment for row in list_review_attachments(db, filters)]


# --- bulk actions --------------------------------------------------------


@dataclass(frozen=True)
class BulkItemResult:
    attachment_id: int
    filename: str
    outcome: str  # "added" / "linked_existing" / "already_reviewed" / "failed"
    document_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class BulkPromotionSummary:
    results: list[BulkItemResult] = field(default_factory=list)

    @property
    def added_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "added")

    @property
    def linked_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "linked_existing")

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "failed")

    @property
    def already_reviewed_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "already_reviewed")


@dataclass(frozen=True)
class BulkPromotionPreview:
    """What "Add Selected"/"Add all recognized" would do, computed
    read-only before anything is committed -- the confirmation summary
    Step 11 requires (e.g. "18 will be added, 4 will link existing
    Documents, 3 remain pending")."""

    will_add_count: int
    will_link_count: int
    not_processable_count: int


def preview_bulk_promotion(db: Session, attachments: list[CommunicationAttachment]) -> BulkPromotionPreview:
    will_add = 0
    will_link = 0
    not_processable = 0
    for attachment in attachments:
        if attachment.review_status != "pending":
            not_processable += 1
            continue
        stored_ok = attachment.communication.case is not None
        if not stored_ok:
            not_processable += 1
            continue
        if _existing_document_for(db, attachment) is not None:
            will_link += 1
        else:
            will_add += 1
    return BulkPromotionPreview(will_add_count=will_add, will_link_count=will_link, not_processable_count=not_processable)


def bulk_add_to_documents(
    db: Session, vault: VaultLayout, attachments: list[CommunicationAttachment], actor: str
) -> BulkPromotionSummary:
    """Promote every attachment in `attachments`, each independently:
    one attachment's failure (or its being a duplicate, which is not a
    failure) never rolls back or blocks any other attachment in the
    same call. Every field is taken from that attachment's own Step 5
    suggestions (`attachment_metadata.py`) and recorded with
    `field_provenance="suggested"` for every suggested field actually
    used -- the exact same provenance value the single-attachment review
    page records when a human submits its pre-filled defaults untouched
    (see communication_attachment_review.html's hidden `*_source`
    inputs) -- so a bulk-accepted default is indistinguishable, in the
    audit trail, from a human individually accepting that same default.
    Skips (records as `"already_reviewed"`, not `"failed"`) any
    attachment no longer `pending` by the time it's processed -- makes
    resubmitting the same bulk action safely idempotent.
    """
    from app.core.communications.attachment_metadata import compose_notes, suggest_date_received, suggest_source

    results: list[BulkItemResult] = []
    for attachment in attachments:
        if attachment.review_status != "pending":
            results.append(
                BulkItemResult(attachment_id=attachment.attachment_id, filename=attachment.filename, outcome="already_reviewed")
            )
            continue

        communication = attachment.communication
        suggested_date = suggest_date_received(communication)
        suggested_src = suggest_source(communication)
        field_provenance = {}
        if attachment.suggested_document_type_id is not None:
            field_provenance["document_type_id"] = "suggested"
        if suggested_src:
            field_provenance["source"] = "suggested"
        if suggested_date is not None:
            field_provenance["date_received"] = "suggested"
        field_provenance["notes"] = "suggested"

        try:
            result: PromotionResult = promote_attachment_to_document(
                db,
                vault,
                attachment,
                document_type_id=attachment.suggested_document_type_id,
                source=suggested_src,
                notes=compose_notes(communication),
                date_received=suggested_date,
                field_provenance=field_provenance,
                actor=actor,
            )
        except (AttachmentNotPromotableError, DuplicateDocumentError) as exc:
            db.rollback()
            results.append(
                BulkItemResult(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    outcome="failed",
                    error=str(exc)[:300],
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 -- one bad attachment must not abort the batch
            db.rollback()
            results.append(
                BulkItemResult(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    outcome="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            continue

        db.commit()
        outcome = "added" if result.created_new_document else "linked_existing"
        results.append(
            BulkItemResult(
                attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                outcome=outcome,
                document_id=result.document.document_id,
            )
        )

    return BulkPromotionSummary(results=results)


def bulk_exclude(db: Session, attachments: list[CommunicationAttachment], actor: str) -> BulkPromotionSummary:
    """Marks each attachment excluded, independently -- `exclude_attachment()`
    is already idempotent (a no-op custody-event-wise on an
    already-excluded attachment), so resubmitting the same selection is
    always safe."""
    results = []
    for attachment in attachments:
        try:
            exclude_attachment(db, attachment, actor)
            db.commit()
        except Exception as exc:  # noqa: BLE001 -- one bad attachment must not abort the batch
            db.rollback()
            results.append(
                BulkItemResult(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    outcome="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            continue
        results.append(
            BulkItemResult(attachment_id=attachment.attachment_id, filename=attachment.filename, outcome="success")
        )
    return BulkPromotionSummary(results=results)


def bulk_leave_with_email(db: Session, attachments: list[CommunicationAttachment], actor: str) -> BulkPromotionSummary:
    """Same shape as `bulk_exclude()` -- see its docstring."""
    results = []
    for attachment in attachments:
        try:
            leave_attachment_with_email(db, attachment, actor)
            db.commit()
        except Exception as exc:  # noqa: BLE001 -- one bad attachment must not abort the batch
            db.rollback()
            results.append(
                BulkItemResult(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    outcome="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            continue
        results.append(
            BulkItemResult(attachment_id=attachment.attachment_id, filename=attachment.filename, outcome="success")
        )
    return BulkPromotionSummary(results=results)
