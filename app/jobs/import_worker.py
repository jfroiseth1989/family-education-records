"""The bulk-import batch queue's worker: claim, process, finalize --
plus crash recovery (Communications Phase Step 10).

Reuses this app's one existing background-execution *pattern* (a single
daemon thread, polling a status column, a fresh `Session` per unit of
work -- see app/jobs/worker.py, Phase 3 Step 0) but is its own separate
module and thread, not a modification of the OCR worker: OCR and
Communications are unrelated domains, and merging their queues would
couple them for no benefit. "One bounded worker is acceptable for
current single-user scale" applies here exactly as it does there.

**Crash recovery is structurally different from the OCR worker's,
deliberately.** `OcrJob` has no sub-row per unit of work, so a job
interrupted mid-run has no persisted partial progress to resume from --
`sweep_stuck_jobs()` simply force-fails any job still `running` at
startup. A `CommunicationImportBatch`, by contrast, already persists
each unit of work as its own `CommunicationImportBatchItem` row,
committed individually as it completes (see `process_next_batch()`
below) -- so a batch a worker was processing when the process died is
still exactly as resumable as any other in-progress batch: whatever
items are `imported`/`skipped_duplicate`/`failed` are untouched, and
`claim_next_batch()` treats `running` as just as claimable as `pending`.
**There is no startup sweep for import batches, and none is needed** --
this is a considered simplification, not an oversight.

**Cancellation has no OCR precedent at all** (grep confirms `cancel`
never appears anywhere in the OCR pipeline). This worker checks the
batch's own `status` -- refreshed from the database, not the possibly-
stale in-memory value -- between every item, so a `cancel` request
written by a web request thread (see
app/core/communications/import_batches.py::request_cancel()) is always
observed before the next message is fetched, never mid-fetch.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.communications.imap_client import (
    ImapAuthenticationError,
    ImapCredentialUnavailableError,
)
from app.core.communications.imap_import import import_one_imap_message
from app.core.communications.imap_service import open_connection
from app.core.communications.import_batches import count_remaining
from app.core.communications.ingestion import DuplicateCommunicationError
from app.core.vault import VaultLayout
from app.db.models import Case, CommunicationAccount, CommunicationImportBatch, CommunicationImportBatchItem

_CLAIMABLE_STATUSES = ("pending", "running")

DEFAULT_POLL_INTERVAL_SECONDS = 2.0

SYSTEM_ACTOR = "system (import batch worker)"

# Truncated the same way OCR page errors are (app/core/ocr/service.py) --
# long enough to be useful, short enough to never balloon a row or leak
# something unbounded (e.g. a pathological exception message) into the
# database.
_MAX_ERROR_TEXT_LENGTH = 300

_logger = logging.getLogger(__name__)


def claim_next_batch(db: Session) -> CommunicationImportBatch | None:
    """The oldest batch with work left to do, if any -- `pending` (never
    started) or `running` (either genuinely mid-processing on a single-
    worker system, which never happens concurrently with this same
    call, or left `running` by an interrupted process -- see module
    docstring for why both are equally safe to claim).
    """
    return db.scalars(
        select(CommunicationImportBatch)
        .where(CommunicationImportBatch.status.in_(_CLAIMABLE_STATUSES))
        .order_by(CommunicationImportBatch.created_at)
    ).first()


def _finalize(db: Session, batch: CommunicationImportBatch) -> None:
    if batch.status == "cancelled":
        return  # a cancel that landed after the last item -- leave it as the user's own terminal choice
    batch.status = "completed_with_errors" if batch.failed_count > 0 else "completed"
    batch.finished_at = datetime.now(timezone.utc)
    db.commit()


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_TEXT_LENGTH]


def process_next_batch(db: Session, vault: VaultLayout) -> CommunicationImportBatch | None:
    """Claim one batch with pending work and process every one of its
    `pending` items, checking for cancellation between each. Returns the
    batch that was worked on, or `None` if nothing was eligible.

    One commit per item -- never one commit for the whole batch -- so a
    crash mid-batch loses at most the one item in flight (rolled back,
    still `pending`, safely retried later), never anything already
    committed. `import_one_imap_message()` only ever flushes (never
    commits) internally, so its `Communication` insert and this item's
    status flip to `imported` always land in the exact same commit --
    there is no window where a `Communication` row exists but its item
    is still `pending`, or vice versa.
    """
    batch = claim_next_batch(db)
    if batch is None:
        return None

    if batch.status == "pending":
        batch.status = "running"
        batch.started_at = datetime.now(timezone.utc)
        db.commit()

    pending_items = list(
        db.scalars(
            select(CommunicationImportBatchItem)
            .where(
                CommunicationImportBatchItem.batch_id == batch.batch_id,
                CommunicationImportBatchItem.status == "pending",
            )
            .order_by(CommunicationImportBatchItem.item_id)
        ).all()
    )

    if not pending_items:
        _finalize(db, batch)
        return batch

    account = db.get(CommunicationAccount, batch.account_id)
    case = db.get(Case, batch.case_id)

    try:
        imap_client = open_connection(account)
    except (ImapAuthenticationError, ImapCredentialUnavailableError) as exc:
        # A true account-level problem -- retrying item-by-item would be
        # pointless (and, for a rejected password, would just hammer
        # Yahoo with more failed logins) and none of these items were
        # ever attempted, so they all stay `pending` untouched. Resuming
        # this batch later (once the account is fixed) picks up exactly
        # where this left off.
        batch.status = "failed"
        db.commit()
        return batch
    except Exception:
        # Network/timeout/etc. opening the connection -- also stop
        # before touching any item, same reasoning, but this kind of
        # failure is worth a plain retry later without requiring the
        # user to fix anything first (still surfaced as `failed` with
        # Resume available, not silently retried forever).
        batch.status = "failed"
        db.commit()
        return batch

    try:
        for item in pending_items:
            db.refresh(batch)
            if batch.status == "cancelled":
                break

            try:
                communication = import_one_imap_message(
                    db,
                    vault,
                    case,
                    imap_client,
                    account_id=batch.account_id,
                    folder=item.mailbox_folder,
                    uid=item.mailbox_uid,
                    actor=SYSTEM_ACTOR,
                )
            except DuplicateCommunicationError as exc:
                db.rollback()
                item.status = "skipped_duplicate"
                item.communication_id = exc.existing_communication.communication_id
                item.updated_at = datetime.now(timezone.utc)
                batch.skipped_duplicate_count += 1
                db.commit()
            except (ImapAuthenticationError, ImapCredentialUnavailableError) as exc:
                # The session died mid-batch (e.g. Yahoo revoked it) --
                # this item was never attempted, so it stays `pending`;
                # stop the whole batch the same way a failure to connect
                # in the first place does.
                db.rollback()
                batch.status = "failed"
                db.commit()
                break
            except Exception as exc:
                # Fetch failure, malformed RFC822, attachment parse/
                # storage failure, a transient network blip on this one
                # message -- isolated to this item; the batch continues.
                db.rollback()
                item.status = "failed"
                item.error = _safe_error_text(exc)
                item.updated_at = datetime.now(timezone.utc)
                batch.failed_count += 1
                db.commit()
            else:
                item.status = "imported"
                item.communication_id = communication.communication_id
                item.updated_at = datetime.now(timezone.utc)
                batch.imported_count += 1
                db.commit()
    finally:
        imap_client.logout()

    db.refresh(batch)
    if batch.status != "cancelled" and count_remaining(db, batch) == 0:
        _finalize(db, batch)

    return batch


def run_import_worker_loop(
    session_factory: sessionmaker[Session],
    vault: VaultLayout,
    stop_event: threading.Event,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """The background thread's main loop: process one batch's pending
    work at a time, forever. Opens a fresh session per pass (never
    shares a Session/connection across threads) and sleeps between polls
    when there's nothing to do. Exits promptly once `stop_event` is set.

    Wraps `process_next_batch()` in a broad `except Exception` -- unlike
    `run_worker_loop()` in app/jobs/worker.py, which has no such guard
    around `process_next_job()` and would take the whole thread down
    with it on an unexpected bug. That gap was noted, not repeated: one
    batch's unexpected failure here is logged and the loop keeps polling
    other batches, rather than silently ending all future imports until
    the next app restart.
    """
    while not stop_event.is_set():
        db = session_factory()
        try:
            batch = process_next_batch(db, vault)
        except Exception:
            _logger.exception("Unexpected error while processing an import batch")
            batch = None
        finally:
            db.close()

        if batch is None:
            stop_event.wait(poll_interval_seconds)
