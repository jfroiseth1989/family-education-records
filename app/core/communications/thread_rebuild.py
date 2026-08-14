"""DB-facing reconciliation of `communication_threads` against the pure
grouping result from `app/core/communications/thread_grouping.py`
(Communications Phase Step 4).

`rebuild_threads()` is a full, deterministic, stateless-from-the-DB's-
perspective rebuild: it reads every non-deleted `Communication`, computes
the partition `group_messages()` says the current data implies, and
reconciles `communication_threads`/`communications.thread_id` to match --
never touching any other field on `Communication` (subject, headers, body,
hash, stored_path, custody events, etc. are all untouched; threading is
grouping/linkage only).

Called synchronously at the end of `import_eml_file()`, inside the same
transaction as the import, so every import leaves the database in a
threading-consistent state -- including retroactively linking a reply
that was imported *before* its parent (the next import's rebuild sees
both rows and reconciles them into one thread) and messages imported out
of chronological order (grouping depends on message content, not import
order -- see `group_messages()`'s docstring).

Idempotent: re-running with no new data recomputes the identical
partition and reuses existing `thread_id`s untouched -- no row churn, no
thread ever "bounces" between ids on a re-run. When two previously
separate threads turn out (only after a new message links them) to be one
conversation, the lower-numbered `thread_id` is kept as canonical and the
other thread row -- now with no members -- is deleted; the underlying
`Communication` rows themselves are never merged, rewritten, or
duplicated, only their `thread_id` column is reassigned.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.thread_grouping import ThreadableMessage, group_messages
from app.db.models import Communication, CommunicationThread


def _naive(value: datetime | None) -> datetime | None:
    """Strip tzinfo without converting, for comparability.

    SQLite has no native timezone type: a `DateTime(timezone=True)` value
    keeps its original tzinfo only until the object is expired and
    re-fetched (e.g. after a `commit()`), at which point SQLAlchemy hands
    back the same wall-clock numbers with tzinfo dropped -- not converted
    to UTC. Within one `rebuild_threads()` call, a communication just
    created in this same transaction is still the aware, never-reloaded
    Python object, while an older sibling fetched by the `select()` below
    may already have gone through that reload -- comparing the two
    directly raises `TypeError: can't compare offset-naive and
    offset-aware datetimes`. Normalizing every date the same way the DB
    itself already normalizes it (drop tzinfo, keep the wall-clock value)
    keeps proximity comparisons internally consistent without silently
    reinterpreting anyone's original UTC offset.
    """
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _to_threadable(row: Communication) -> ThreadableMessage:
    return ThreadableMessage(
        communication_id=row.communication_id,
        message_id=row.message_id_header,
        in_reply_to=row.in_reply_to_header,
        references=tuple(row.references_header or ()),
        subject=row.subject,
        from_address=row.from_address,
        to_addresses=tuple(row.to_addresses or ()),
        cc_addresses=tuple(row.cc_addresses or ()),
        message_date=_naive(row.sent_at or row.received_at),
        case_id=row.case_id,
    )


def rebuild_threads(db: Session) -> None:
    """Recompute thread grouping over every non-deleted communication and
    reconcile `communication_threads`/`communications.thread_id` to match.

    Does not commit; the caller controls the transaction boundary (same
    convention as `import_eml_file()` and `write_communication_custody_event()`).
    """
    rows = list(db.scalars(select(Communication).where(Communication.deleted_at.is_(None))).all())
    by_id = {row.communication_id: row for row in rows}

    groups = group_messages([_to_threadable(row) for row in rows])

    used_thread_ids: set[int] = set()
    grouped_communication_ids: set[int] = set()

    for group in groups:
        grouped_communication_ids.update(group.communication_ids)

        existing_thread_ids = sorted(
            {
                by_id[cid].thread_id
                for cid in group.communication_ids
                if by_id[cid].thread_id is not None
            }
        )
        if existing_thread_ids:
            canonical_id = existing_thread_ids[0]
            thread = db.get(CommunicationThread, canonical_id)
        else:
            thread = CommunicationThread()
            db.add(thread)
            db.flush()  # assigns thread.thread_id
            canonical_id = thread.thread_id

        thread.subject_normalized = group.subject_normalized
        thread.participant_summary = ", ".join(group.participants) if group.participants else None
        thread.first_message_at = group.first_message_at
        thread.last_message_at = group.last_message_at
        thread.message_count = len(group.communication_ids)
        thread.case_id = group.case_id

        for cid in group.communication_ids:
            row = by_id[cid]
            if row.thread_id != canonical_id:
                row.thread_id = canonical_id

        used_thread_ids.add(canonical_id)

    # Any communication not part of a real (2+) conversation this rebuild
    # -- including one whose thread shrank to a single member because its
    # sibling(s) were soft-deleted -- goes back to unthreaded.
    for row in rows:
        if row.communication_id not in grouped_communication_ids and row.thread_id is not None:
            row.thread_id = None

    db.flush()

    all_thread_ids = set(db.scalars(select(CommunicationThread.thread_id)).all())
    for stale_id in all_thread_ids - used_thread_ids:
        stale_thread = db.get(CommunicationThread, stale_id)
        if stale_thread is not None:
            db.delete(stale_thread)

    db.flush()
