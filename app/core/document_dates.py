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
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.db.models import Document, DocumentDatePrecision, DocumentDateSource


def set_document_date(
    document: Document,
    document_date: date | None,
    source: DocumentDateSource,
    precision: DocumentDatePrecision = DocumentDatePrecision.EXACT,
) -> None:
    """Set, update, or clear a document's own effective date.

    Passing ``document_date=None`` clears the date along with its source
    and precision — the "unknown/unavailable" case. When a date *is*
    given, ``source`` is required (never inferred) so it's always explicit
    whether a date was entered by a person, extracted from document text,
    or read from file metadata — see ``DocumentDateSource``.
    """
    if document_date is None:
        document.document_date = None
        document.document_date_source = None
        document.document_date_precision = None
        return

    document.document_date = datetime.combine(document_date, time.min, tzinfo=timezone.utc)
    document.document_date_source = source.value
    document.document_date_precision = precision.value
