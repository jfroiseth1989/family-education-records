"""Bulk-import batch lifecycle: create, cancel, resume, retry-failed
(Communications Phase Step 10).

Pure database bookkeeping -- nothing here ever opens an IMAP connection
or touches Yahoo; the background worker
(app/core/communications/import_worker.py) is the only thing that does
that, driven by the state this module writes. Keeping the two apart
means every function here is trivially testable without a fake IMAP
transport at all.

Batch status vocabulary (`CommunicationImportBatch.status`): `pending`
(created, not yet started) -> `running` (the worker is actively working
through its items) -> a terminal state of `completed` (every item
succeeded or was a duplicate), `completed_with_errors` (every item was
processed but at least one failed), `cancelled` (the user stopped it;
whatever items were still `pending` stay `pending` forever unless a
future batch re-selects the same messages), or `failed` (a true
account-level problem -- bad/missing credentials -- stopped the worker
before it could finish; unlike `cancelled`, `failed` is meant to be
retried once the underlying account problem is fixed, via
`resume_batch()`). `paused` is deliberately never produced by this
application: this batch design's per-item persistence
(`CommunicationImportBatchItem`) already makes an app-restart-interrupted
batch safely resumable without a distinct "paused" state -- a batch left
`running` when the process died is simply re-claimed by the worker on
its very next poll after restart (see import_worker.py's module
docstring for the full reasoning). The column comment still lists
`paused` as part of the documented vocabulary in case a future step
finds a genuine use for it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Case, CommunicationAccount, CommunicationImportBatch, CommunicationImportBatchItem

# Statuses from which resuming (re-arming for the worker to pick back up)
# makes sense -- both represent unintended stops, never a deliberate
# "I'm done with this" like `cancelled`.
_RESUMABLE_STATUSES = ("failed",)

# Statuses from which a user-initiated cancel is meaningful.
_CANCELLABLE_STATUSES = ("pending", "running", "failed")


class BatchValidationError(ValueError):
    """Raised when a batch cannot be created as requested -- e.g. no
    student selected, or no messages selected. Never wraps an IMAP or
    database error; this is purely a request-shape problem, caught by
    the route and shown as a normal validation message.
    """


def create_batch(
    db: Session,
    account: CommunicationAccount,
    case: Case | None,
    items: list[tuple[str, str]],
    search_criteria: dict,
    actor: str,
) -> CommunicationImportBatch:
    """Create a batch and persist its full work queue up front, before
    any processing begins -- this is what makes the batch resumable:
    every selected `(folder, uid)` becomes its own `pending`
    `CommunicationImportBatchItem` row in the same transaction as the
    batch itself, so the worker never has to re-derive "what was
    selected" from anything other than these rows.

    Requires `case` (a batch-level default student every successfully
    imported message is assigned to -- Step 10's explicit requirement)
    and at least one selected item; raises `BatchValidationError`
    (nothing written) otherwise. Selected `(folder, uid)` pairs are
    de-duplicated defensively -- a manipulated or double-submitted form
    could otherwise list the same message twice, which would violate the
    batch's own `(batch_id, mailbox_folder, mailbox_uid)` uniqueness.
    Commits once, atomically, on success.
    """
    if case is None:
        raise BatchValidationError("A student must be selected before starting an import.")

    unique_items: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for folder, uid in items:
        key = (folder, uid)
        if key not in seen:
            seen.add(key)
            unique_items.append(key)

    if not unique_items:
        raise BatchValidationError("Select at least one message to import.")

    batch = CommunicationImportBatch(
        account_id=account.account_id,
        case_id=case.case_id,
        search_criteria=search_criteria,
        status="pending",
        matched_count=len(unique_items),
        created_by=actor,
    )
    db.add(batch)
    db.flush()  # assigns batch.batch_id

    for folder, uid in unique_items:
        db.add(
            CommunicationImportBatchItem(
                batch_id=batch.batch_id, mailbox_folder=folder, mailbox_uid=uid, status="pending"
            )
        )

    db.commit()
    return batch


def request_cancel(db: Session, batch: CommunicationImportBatch) -> None:
    """Stop future work on `batch` -- never rolls back anything already
    imported. Whatever items are still `pending` when the worker next
    checks stay `pending` indefinitely (not touched, not marked
    `failed`) -- this is a deliberate stop, not an error.
    """
    if batch.status in _CANCELLABLE_STATUSES:
        batch.status = "cancelled"
        db.commit()


def resume_batch(db: Session, batch: CommunicationImportBatch) -> None:
    """Re-arm a `failed` batch (an account-level problem, e.g. a
    rejected app password, presumably now fixed) so the worker picks it
    back up. Never touches any `CommunicationImportBatchItem` row --
    whatever is `imported`/`skipped_duplicate`/`failed` stays exactly as
    it is; only items still `pending` will be attempted.
    """
    if batch.status in _RESUMABLE_STATUSES:
        batch.status = "pending"
        db.commit()


def retry_failed_items(db: Session, batch: CommunicationImportBatch) -> int:
    """Reset every `failed` item on `batch` back to `pending` (clearing
    its recorded error) so the worker retries it, without touching
    `imported`/`skipped_duplicate` items at all. Returns the number of
    items reset. Safe to call on a batch with zero failed items (a
    no-op, not an error).
    """
    failed_items = list(
        db.scalars(
            select(CommunicationImportBatchItem).where(
                CommunicationImportBatchItem.batch_id == batch.batch_id,
                CommunicationImportBatchItem.status == "failed",
            )
        ).all()
    )
    if not failed_items:
        return 0

    for item in failed_items:
        item.status = "pending"
        item.error = None
        item.updated_at = None

    batch.failed_count = max(0, batch.failed_count - len(failed_items))
    if batch.status in ("completed_with_errors", "failed", "cancelled"):
        batch.status = "pending"

    db.commit()
    return len(failed_items)


def count_remaining(db: Session, batch: CommunicationImportBatch) -> int:
    """How many items on `batch` are still `pending` -- used by both the
    worker (to decide whether to finalize) and the status page (to show
    "N remaining").
    """
    return db.scalar(
        select(func.count())
        .select_from(CommunicationImportBatchItem)
        .where(
            CommunicationImportBatchItem.batch_id == batch.batch_id,
            CommunicationImportBatchItem.status == "pending",
        )
    )
