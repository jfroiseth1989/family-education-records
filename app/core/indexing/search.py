"""Full-text search over extracted document text.

Queries the `document_text_fts` FTS5 index (see the migration
`08c778ee32af` for how it's kept in sync with `document_pages` via
triggers) and resolves every hit back to its source document and exact
page — that's the whole point of building this on `document_pages`
rather than a free-floating index: a search result is never just text,
it's always a citable location. Case-scoped only; no cross-case search
in Phase 2 — see docs/PHASE_2_PLAN.md §6.
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


def search_case_documents(
    db: Session,
    case_id: int,
    query: str,
    *,
    document_type_id: int | None = None,
    needs_ocr: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
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
        )
        for row in rows
    ]


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
