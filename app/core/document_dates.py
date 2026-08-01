"""Setting a document's own effective date.

`Document.document_date` is the date *on* the record itself — e.g. the
date printed on a letter, the date an evaluation was conducted, the date
an IEP was signed. It is deliberately kept separate from any filesystem or
ingestion timestamp (`Document.ingested_at`, or the uploaded file's OS
modification time, which this module never reads): those reflect when the
app happened to see the file, not what the record is about. Conflating the
two would misrepresent the document's actual date in the timeline this
application is ultimately meant to support (see docs/PHASE_1_REVIEW.md
item 4).

A document's date is frequently unknown at import time. That is a fully
valid, expected state — not an error — and is represented by simply
leaving `document_date` (and its source/precision) unset.

Special education records also routinely carry a genuine date *range*
(a triennial evaluation window, "sometime in March 2024") rather than a
single known day — see `DocumentDatePrecision.RANGE` and
docs/DATA_MODEL.md "Date representation pattern."
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.db.models import Document, DocumentDatePrecision, DocumentDateSource


class InvalidDateRangeError(ValueError):
    """Raised when a document date's range_end doesn't match its precision."""


def validate_date_combination(
    document_date: date | None,
    precision: DocumentDatePrecision,
    range_end: date | None,
) -> None:
    """Raise :class:`InvalidDateRangeError` if ``range_end`` is inconsistent
    with ``precision``, otherwise do nothing. Pure validation, no side effects.

    Callers that need to fail fast *before* doing any filesystem or
    database work — notably ingestion, which would otherwise copy a file
    into the vault before discovering a bad date combination, leaving an
    orphaned file with no corresponding document row — should call this
    directly, up front, rather than relying solely on
    :func:`set_document_date`'s identical check, which only runs once a
    ``Document`` instance already exists to mutate.
    """
    if document_date is None:
        return
    if precision == DocumentDatePrecision.RANGE:
        if range_end is None:
            raise InvalidDateRangeError(
                "A range end date is required when precision is 'range'."
            )
        if range_end < document_date:
            raise InvalidDateRangeError(
                "A date range's end must be on or after its start."
            )
    elif range_end is not None:
        raise InvalidDateRangeError(
            f"range_end was given but precision is '{precision.value}', not 'range'."
        )


def set_document_date(
    document: Document,
    document_date: date | None,
    source: DocumentDateSource,
    precision: DocumentDatePrecision = DocumentDatePrecision.EXACT,
    range_end: date | None = None,
) -> None:
    """Set, update, or clear a document's own effective date.

    Passing ``document_date=None`` clears the date along with its source,
    precision, and range end — the "unknown/unavailable" case. When a date
    *is* given, ``source`` is required (never inferred) so it's always
    explicit whether a date was entered by a person, extracted from
    document text, or read from file metadata — see ``DocumentDateSource``.

    ``range_end`` is meaningful only when ``precision`` is
    ``DocumentDatePrecision.RANGE``: it's required in that case (and must
    fall on or after ``document_date``, which holds the range's start),
    and must be omitted for any other precision — see
    :func:`validate_date_combination`, called here as a safety net even
    though callers with filesystem side effects (ingestion) should also
    call it themselves up front.
    """
    if document_date is None:
        document.document_date = None
        document.document_date_range_end = None
        document.document_date_source = None
        document.document_date_precision = None
        return

    validate_date_combination(document_date, precision, range_end)

    document.document_date = datetime.combine(document_date, time.min, tzinfo=timezone.utc)
    document.document_date_range_end = (
        datetime.combine(range_end, time.min, tzinfo=timezone.utc)
        if range_end is not None
        else None
    )
    document.document_date_source = source.value
    document.document_date_precision = precision.value
