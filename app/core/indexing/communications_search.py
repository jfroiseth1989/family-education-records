"""Full-text and structured search over imported Communications
(Communications Phase Step 6).

Queries the `communication_text_fts` FTS5 index (migration `34b632653500`,
kept in sync via triggers) for the free-text query only -- subject and
parsed plain-text body. Every other filter (sender, recipient, CC,
subject, date range, has-attachments, case, threaded/unthreaded) is a
plain SQL condition against `communications`/`communication_attachments`
directly, never forced through FTS5 -- exactly the same division of
labor as `app/core/indexing/search.py`'s Document search, and entirely
independent of it: this module never touches `document_text_fts` or
`annotation_notes_fts`, and nothing here changes how Document search
behaves.

Recipient/CC filtering uses SQLite's `json_each()` table-valued function
against the `to_addresses`/`cc_addresses` JSON columns -- those are
stored as JSON arrays of address strings (see
`app/core/extraction/email.py`), not flat text, so a plain `LIKE` against
the column itself would match on serialized JSON punctuation as much as
an actual address.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.indexing.search import _quote_as_phrase


@dataclass(frozen=True)
class CommunicationSearchResult:
    communication_id: int
    subject: str | None
    from_display_name: str | None
    from_address: str | None
    sent_at: datetime | None
    case_id: int | None
    case_display_name: str | None
    has_attachments: bool
    thread_id: int | None
    snippet: str | None


def search_communications(
    db: Session,
    *,
    query: str = "",
    case_id: int | None = None,
    sender: str = "",
    recipient: str = "",
    cc: str = "",
    subject: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    has_attachments: bool | None = None,
    threaded: bool | None = None,
    limit: int = 100,
) -> list[CommunicationSearchResult]:
    """Search imported communications by free text and/or structured filters.

    At least one of ``query`` or a structured filter must be given, or
    this returns an empty list without touching the database at all --
    same "query tool, not a browse-everything view" convention as
    Document search, just extended to "or a filter," since Communications
    search is explicitly meant to support filter-only lookups (e.g. "every
    email from this sender") with no free text at all.
    """
    stripped_query = query.strip()
    stripped_sender = sender.strip()
    stripped_recipient = recipient.strip()
    stripped_cc = cc.strip()
    stripped_subject = subject.strip()

    has_any_filter = any(
        [
            stripped_query,
            case_id is not None,
            stripped_sender,
            stripped_recipient,
            stripped_cc,
            stripped_subject,
            date_from is not None,
            date_to is not None,
            has_attachments is not None,
            threaded is not None,
        ]
    )
    if not has_any_filter:
        return []

    conditions = ["c.deleted_at IS NULL"]
    params: dict[str, object] = {"limit": limit}

    if case_id is not None:
        conditions.append("c.case_id = :case_id")
        params["case_id"] = case_id

    if stripped_sender:
        conditions.append("(c.from_address LIKE :sender_pattern OR c.from_display_name LIKE :sender_pattern)")
        params["sender_pattern"] = f"%{stripped_sender}%"

    if stripped_recipient:
        conditions.append(
            "EXISTS (SELECT 1 FROM json_each(c.to_addresses) WHERE json_each.value LIKE :recipient_pattern)"
        )
        params["recipient_pattern"] = f"%{stripped_recipient}%"

    if stripped_cc:
        conditions.append(
            "EXISTS (SELECT 1 FROM json_each(c.cc_addresses) WHERE json_each.value LIKE :cc_pattern)"
        )
        params["cc_pattern"] = f"%{stripped_cc}%"

    if stripped_subject:
        conditions.append("c.subject LIKE :subject_pattern")
        params["subject_pattern"] = f"%{stripped_subject}%"

    date_condition = _build_date_range_condition(date_from, date_to, params)
    if date_condition:
        conditions.append(date_condition)

    if has_attachments is True:
        conditions.append(
            "EXISTS (SELECT 1 FROM communication_attachments ca WHERE ca.communication_id = c.communication_id)"
        )
    elif has_attachments is False:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM communication_attachments ca WHERE ca.communication_id = c.communication_id)"
        )

    if threaded is True:
        conditions.append("c.thread_id IS NOT NULL")
    elif threaded is False:
        conditions.append("c.thread_id IS NULL")

    where_clause = " AND ".join(conditions)

    if stripped_query:
        params["match_query"] = _quote_as_phrase(stripped_query)
        sql = text(
            f"""
            SELECT
                c.communication_id AS communication_id,
                c.subject AS subject,
                c.from_display_name AS from_display_name,
                c.from_address AS from_address,
                c.sent_at AS sent_at,
                c.case_id AS case_id,
                cs.label AS case_display_name,
                c.thread_id AS thread_id,
                EXISTS (
                    SELECT 1 FROM communication_attachments ca
                    WHERE ca.communication_id = c.communication_id
                ) AS has_attachments,
                snippet(communication_text_fts, -1, '[', ']', '…', 12) AS snippet
            FROM communication_text_fts
            JOIN communications c ON c.communication_id = communication_text_fts.rowid
            LEFT JOIN cases cs ON cs.case_id = c.case_id
            WHERE communication_text_fts MATCH :match_query
              AND {where_clause}
            ORDER BY rank
            LIMIT :limit
            """  # noqa: S608 -- where_clause is built only from fixed,
            # hardcoded fragments above, never from raw user input; every
            # actual value is bound as a parameter.
        )
    else:
        sql = text(
            f"""
            SELECT
                c.communication_id AS communication_id,
                c.subject AS subject,
                c.from_display_name AS from_display_name,
                c.from_address AS from_address,
                c.sent_at AS sent_at,
                c.case_id AS case_id,
                cs.label AS case_display_name,
                c.thread_id AS thread_id,
                EXISTS (
                    SELECT 1 FROM communication_attachments ca
                    WHERE ca.communication_id = c.communication_id
                ) AS has_attachments,
                NULL AS snippet
            FROM communications c
            LEFT JOIN cases cs ON cs.case_id = c.case_id
            WHERE {where_clause}
            ORDER BY c.sent_at DESC, c.imported_at DESC
            LIMIT :limit
            """  # noqa: S608 -- same as above.
        )

    rows = db.execute(sql, params).mappings().all()
    return [
        CommunicationSearchResult(
            communication_id=row["communication_id"],
            subject=row["subject"],
            from_display_name=row["from_display_name"],
            from_address=row["from_address"],
            sent_at=row["sent_at"],
            case_id=row["case_id"],
            case_display_name=row["case_display_name"],
            has_attachments=bool(row["has_attachments"]),
            thread_id=row["thread_id"],
            snippet=row["snippet"],
        )
        for row in rows
    ]


def _build_date_range_condition(
    date_from: date | None, date_to: date | None, params: dict[str, object]
) -> str | None:
    """Build the WHERE fragment for the date-range filter, matched against
    a message's own sent date -- falling back to its received date only
    when sent is unknown, since that's the closest available substitute
    for "when this message happened."

    Unlike a `Document`'s date (always normalized to UTC midnight, see
    `app/core/document_dates.py`), a communication's `sent_at`/`received_at`
    carries a real time-of-day parsed from the message's own headers -- an
    inclusive `<= midnight-of-date_to` bound would wrongly exclude every
    message sent later than 00:00:00 on the last day of the window. The
    upper bound is instead an exclusive `< midnight-of-the-day-after`.
    """
    if date_from is None and date_to is None:
        return None

    conditions = ["COALESCE(c.sent_at, c.received_at) IS NOT NULL"]
    if date_from is not None:
        params["date_from"] = _to_utc_midnight(date_from)
        conditions.append("COALESCE(c.sent_at, c.received_at) >= :date_from")
    if date_to is not None:
        params["date_to_exclusive"] = _to_utc_midnight(date_to + timedelta(days=1))
        conditions.append("COALESCE(c.sent_at, c.received_at) < :date_to_exclusive")
    return " AND ".join(conditions)


def _to_utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)
