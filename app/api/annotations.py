"""Document viewer + annotation routes: highlights, notes, bookmarks, and
notes search.

See docs/PHASE_2_PLAN.md §7/§13 Step 4. The viewer renders one page's
extracted text at a time with lightweight text-offset highlighting (the
approved v1 scope — not full PDF.js spatial rendering).

The notes-search route (Step 5, §13) is a separate page from document text
search (app/api/search.py) — deliberately not merged into the same results
list; see app/core/indexing/notes_search.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.annotations.service import (
    InvalidHighlightRangeError,
    create_bookmark,
    create_highlight,
    create_note,
    list_page_annotations,
    remove_annotation,
)
from app.core.indexing.notes_search import search_case_annotation_notes
from app.db.models import Annotation, Case, Document, DocumentPage

router = APIRouter(tags=["annotations"])


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    return case


def _get_page_by_id_or_404(db: Session, page_id: int) -> DocumentPage:
    page = db.get(DocumentPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id} not found.")
    return page


@router.get("/documents/{document_id}/view", response_class=HTMLResponse)
def view_document(
    request: Request, document_id: int, page: int = 1, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the document viewer for one page: text, highlights, notes, bookmarks."""
    document = _get_document_or_404(db, document_id)

    pages = sorted(document.pages, key=lambda p: p.page_number)
    current_page = next((p for p in pages if p.page_number == page), None)

    annotations = (
        list_page_annotations(db, document_id, current_page.page_id)
        if current_page is not None
        else []
    )
    highlights = [a for a in annotations if a.annotation_type.name == "highlight"]
    notes = [a for a in annotations if a.annotation_type.name == "note"]
    bookmarks = [a for a in annotations if a.annotation_type.name == "bookmark"]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "document_viewer.html",
        {
            "document": document,
            "pages": pages,
            "current_page": current_page,
            "current_page_number": page,
            "highlights": highlights,
            "notes": notes,
            "bookmarks": bookmarks,
        },
    )


@router.post("/documents/{document_id}/annotations/highlight")
def add_highlight(
    document_id: int,
    page_id: int = Form(...),
    start_offset: int = Form(...),
    end_offset: int = Form(...),
    color: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)
    page_row = _get_page_by_id_or_404(db, page_id)
    if page_row.document_id != document_id:
        raise HTTPException(status_code=400, detail="Page does not belong to this document.")

    try:
        create_highlight(
            db, document, page_row, start_offset, end_offset, actor=actor,
            color=color.strip() or None,
        )
    except InvalidHighlightRangeError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(
        url=f"/documents/{document_id}/view?page={page_row.page_number}", status_code=303
    )


@router.post("/documents/{document_id}/annotations/note")
def add_note(
    document_id: int,
    page_id: int = Form(...),
    body_text: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)
    page_row = _get_page_by_id_or_404(db, page_id)
    if page_row.document_id != document_id:
        raise HTTPException(status_code=400, detail="Page does not belong to this document.")

    try:
        create_note(db, document, page_row, body_text, actor=actor)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(
        url=f"/documents/{document_id}/view?page={page_row.page_number}", status_code=303
    )


@router.post("/documents/{document_id}/annotations/bookmark")
def add_bookmark(
    document_id: int,
    page_id: int = Form(...),
    body_text: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)
    page_row = _get_page_by_id_or_404(db, page_id)
    if page_row.document_id != document_id:
        raise HTTPException(status_code=400, detail="Page does not belong to this document.")

    create_bookmark(db, document, page_row, actor=actor, body_text=body_text or None)
    db.commit()
    return RedirectResponse(
        url=f"/documents/{document_id}/view?page={page_row.page_number}", status_code=303
    )


@router.post("/documents/{document_id}/annotations/{annotation_id}/remove")
def remove_annotation_route(
    document_id: int,
    annotation_id: int,
    page: int = 1,
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    _get_document_or_404(db, document_id)
    annotation = db.get(Annotation, annotation_id)
    if annotation is None or annotation.document_id != document_id:
        raise HTTPException(status_code=404, detail=f"Annotation {annotation_id} not found.")

    remove_annotation(db, annotation, actor=actor)
    db.commit()
    return RedirectResponse(url=f"/documents/{document_id}/view?page={page}", status_code=303)


@router.get("/cases/{case_id}/notes-search", response_class=HTMLResponse)
def search_case_notes(
    request: Request, case_id: int, q: str = Query(""), db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the notes-search form and, if a query is present, its results.

    Searches only `annotations.body_text` (notes and bookmarks) — never
    document text, and never mixed into the same result list as
    /cases/{case_id}/search. See app/core/indexing/notes_search.py.
    """
    case = _get_case_or_404(db, case_id)

    results = search_case_annotation_notes(db, case_id, q)

    rows = []
    for result in results:
        document = db.get(Document, result.document_id)
        page = db.get(DocumentPage, result.page_id) if result.page_id is not None else None
        rows.append(
            {
                "annotation_type": result.annotation_type,
                "snippet": result.snippet,
                "document_id": result.document_id,
                "original_filename": document.original_filename if document else None,
                "page_number": page.page_number if page else None,
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "notes_search.html",
        {
            "case": case,
            "query": q,
            "results": rows,
            "searched": bool(q.strip()),
        },
    )
