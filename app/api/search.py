"""Search routes: a case-scoped full-text search page.

See docs/PHASE_2_PLAN.md §6. Search is a query tool, not a browse-all
view — an empty query renders the form with no results rather than
listing every page in the case.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.indexing.search import search_case_documents
from app.db.models import Case, DocumentType

router = APIRouter(tags=["search"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    return case


def _parse_optional_date(raw: str | None, field_label: str) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {field_label} '{raw}' (expected YYYY-MM-DD)."
        ) from exc


@router.get("/cases/{case_id}/search", response_class=HTMLResponse)
def search_case(
    request: Request,
    case_id: int,
    q: str = Query(""),
    document_type_id: str = Query(""),
    needs_ocr: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the search form and, if a query is present, its results."""
    case = _get_case_or_404(db, case_id)

    parsed_type_id = int(document_type_id) if document_type_id.strip() else None
    parsed_needs_ocr = (
        needs_ocr == "true" if needs_ocr in ("true", "false") else None
    )
    parsed_date_from = _parse_optional_date(date_from.strip() or None, "date_from")
    parsed_date_to = _parse_optional_date(date_to.strip() or None, "date_to")

    results = search_case_documents(
        db,
        case_id,
        q,
        document_type_id=parsed_type_id,
        needs_ocr=parsed_needs_ocr,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
    )

    document_types = db.scalars(
        select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "case": case,
            "query": q,
            "results": results,
            "document_types": document_types,
            "selected_document_type_id": parsed_type_id,
            "selected_needs_ocr": needs_ocr,
            "date_from": date_from,
            "date_to": date_to,
            "searched": bool(q.strip()),
        },
    )
