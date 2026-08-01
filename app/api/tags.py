"""Tag routes: attach/detach a tag on a document.

See docs/PHASE_2_PLAN.md §13 Step 3. No rename/delete-tag routes exist —
see app/core/tagging.py for why that's a deliberate Step 3 boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.tagging import tag_document, untag_document
from app.db.models import Document

router = APIRouter(tags=["tags"])


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


@router.post("/documents/{document_id}/tags")
def add_tag(
    document_id: int,
    name: str = Form(...),
    category: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)

    if not name.strip():
        raise HTTPException(status_code=400, detail="Tag name is required.")

    tag_document(db, document, name, category.strip() or None, actor=actor)
    db.commit()
    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.post("/documents/{document_id}/tags/{tag_id}/remove")
def remove_tag(
    document_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)

    untag_document(db, document, tag_id, actor=actor)
    db.commit()
    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)
