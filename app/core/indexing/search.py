"""Full-text search over extracted document text.

Queries the `document_text_fts` FTS5 index (see the migration
`08c778ee32af` for how it's kept in sync with `document_pages` via
triggers) and resolves every hit back to its source document and exact
page — that's the whole point of building this on `document_pages`
rather than a free-floating index: a search result is never just text,
it's always a citable location. Case-scoped only; no cross-case search
in Phase 2 — see docs/PHASE_2_PLAN.md §6.

`SearchResult.extraction_method` (Phase 3 Step 2) is a straight passthrough
of `document_pages.extraction_method` -- added so the UI can show a
provenance badge (native vs. OCR) per result, per
docs/PHASE_3_IMPLEMENTATION_PLAN.md §8. Verified this needed no change to
`document_text_fts` itself or its sync triggers -- those already cover
`ocr_text` since Phase 2 Step 2 (see tests/test_ocr_search_integration.py).

`reindex_page_ocr_text()` (Phase 3 Step 3) is the one exception to
"triggers handle all of document_text_fts's syncing" -- a correction
(app/core/ocr/corrections.py) needs the *search index* to reflect the
corrected text without ever touching `document_pages.ocr_text` itself
(docs/PRIVACY_SECURITY.md §3 locks raw OCR output as never-overwritten).
Extends Step 5's proven "explicit reindex, no trigger" pattern
(app/core/indexing/notes_search.py) to this table for this one case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SearchResult:
    document_id: int
    page_id: int
    page_number: int
    original_filename: str
    snippet: str
    extraction_method: str  # native / ocr / none -- see document_pages.extraction_method


def search_case_documents(
    db: Session,
    case_id: int,
    query: str,
    *,
    document_type_id: int | None = None,
    needs_ocr: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    tag_id: int | None = None,
    limit: int = 50,
) -> list[SearchResult]:
    """Search extracted text within one case, with optional filters.

    ``query`` must be non-empty — an empty query returns no results rather
    than attempting to list every page in the case (search is a query
    tool here, not a browse-everything view; see docs/PHASE_2_PLAN.md §6).
    """
    stripped_query = query.strip()
    if not stripped_query:
        return []

    conditions = ["d.case_id = :case_id", "d.deleted_at IS NULL"]
    params: dict[str, object] = {
        "case_id": case_id,
        "match_query": _quote_as_phrase(stripped_query),
        "limit": limit,
    }

    if document_type_id is not None:
        conditions.append("d.document_type_id = :document_type_id")
        params["document_type_id"] = document_type_id

    if needs_ocr is not None:
        conditions.append("d.needs_ocr = :needs_ocr")
        params["needs_ocr"] = needs_ocr

    if tag_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM document_tags dt "
            "WHERE dt.document_id = d.document_id AND dt.tag_id = :tag_id)"
        )
        params["tag_id"] = tag_id

    date_condition = _build_date_range_condition(date_from, date_to, params)
    if date_condition:
        conditions.append(date_condition)

    where_clause = " AND ".join(conditions)

    sql = text(
        f"""
        SELECT
            dp.document_id AS document_id,
            dp.page_id AS page_id,
            dp.page_number AS page_number,
            d.original_filename AS original_filename,
            dp.extraction_method AS extraction_method,
            snippet(document_text_fts, -1, '[', ']', '…', 10) AS snippet
        FROM document_text_fts
        JOIN document_pages dp ON dp.page_id = document_text_fts.rowid
        JOIN documents d ON d.document_id = dp.document_id
        WHERE document_text_fts MATCH :match_query
          AND {where_clause}
        ORDER BY rank
        LIMIT :limit
        """  # noqa: S608 -- where_clause is built from a fixed set of
        # hardcoded fragments above, never from raw user input; all actual
        # values are bound as parameters.
    )

    rows = db.execute(sql, params).mappings().all()
    return [
        SearchResult(
            document_id=row["document_id"],
            page_id=row["page_id"],
            page_number=row["page_number"],
            original_filename=row["original_filename"],
            snippet=row["snippet"],
            extraction_method=row["extraction_method"],
        )
        for row in rows
    ]


def reindex_page_ocr_text(
    db: Session, page_id: int, *, extracted_text: str | None, previous_ocr_value: str | None, new_ocr_value: str | None
) -> None:
    """Explicitly override `document_text_fts`'s indexed `ocr_text` value
    for one page, without touching `document_pages.ocr_text` itself.

    ``previous_ocr_value`` must be exactly what's *currently* indexed for
    this page's `ocr_text` column -- FTS5's external-content `'delete'`
    command needs the value it's removing to correctly locate that
    value's terms in the index. This is **not** always
    `document_pages.ocr_text` (the real column, permanently raw): after a
    first correction, what's currently indexed is that correction's text,
    not the raw OCR text underneath it. The caller
    (app/core/ocr/corrections.py::create_ocr_correction) is responsible
    for passing whatever `effective_text()` resolved to *immediately
    before* this call, which is exactly the previously-indexed value by
    construction, however many corrections deep.

    ``extracted_text`` is passed through unchanged on both the delete and
    insert steps -- native text is never affected by a correction, and
    native/OCR are mutually exclusive per page, so this is always
    whatever `document_pages.extracted_text` already is (typically
    `None`, for a page that needed OCR at all).
    """
    db.execute(
        text(
            "INSERT INTO document_text_fts(document_text_fts, rowid, extracted_text, ocr_text) "
            "VALUES ('delete', :rowid, :extracted_text, :previous_ocr)"
        ),
        {"rowid": page_id, "extracted_text": extracted_text, "previous_ocr": previous_ocr_value},
    )
    db.execute(
        text(
            "INSERT INTO document_text_fts(rowid, extracted_text, ocr_text) "
            "VALUES (:rowid, :extracted_text, :new_ocr)"
        ),
        {"rowid": page_id, "extracted_text": extracted_text, "new_ocr": new_ocr_value},
    )


def _quote_as_phrase(query: str) -> str:
    """Turn free-typed user input into a safe FTS5 phrase query.

    Wrapping in double quotes (escaping any internal quotes by doubling —
    FTS5's own escaping convention) means the user's text is always
    treated as a literal phrase, never as FTS5 query syntax (AND/OR/NOT,
    unbalanced quotes, column filters, etc.) that could otherwise raise a
    syntax error on ordinary-looking input like `won't` or `"quoted"`.
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def _build_date_range_condition(
    date_from: date | None, date_to: date | None, params: dict[str, object]
) -> str | None:
    """Build the WHERE fragment for the (range-aware) document date filter.

    A document's own date is either a single day (exact/approximate
    precision) or a span (document_date .. document_date_range_end, for
    precision="range" — see docs/DATA_MODEL.md "Date representation
    pattern"). Matching the filter window [date_from, date_to] means:
      - exact/approximate: the single date falls within the window.
      - range: the document's date span overlaps the window at all.
    Either bound may be omitted (open-ended on that side). Returns None
    (no condition) if neither bound is given.
    """
    if date_from is None and date_to is None:
        return None

    single_conditions = ["d.document_date_precision != 'range'"]
    range_conditions = ["d.document_date_precision = 'range'"]

    if date_from is not None:
        params["date_from"] = _to_utc_midnight(date_from)
        single_conditions.append("d.document_date >= :date_from")
        # Overlap check: the range's end must be on/after the window start.
        range_conditions.append("d.document_date_range_end >= :date_from")

    if date_to is not None:
        params["date_to"] = _to_utc_midnight(date_to)
        single_conditions.append("d.document_date <= :date_to")
        # Overlap check: the range's start must be on/before the window end.
        range_conditions.append("d.document_date <= :date_to")

    single_clause = " AND ".join(single_conditions)
    range_clause = " AND ".join(range_conditions)
    return f"d.document_date IS NOT NULL AND (({single_clause}) OR ({range_clause}))"


def _to_utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)
