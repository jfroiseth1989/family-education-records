"""Fact & observation routes (Phase 3.5).

See docs/ARCHITECTURE.md §3.7. Step 3 adds the one route this step
needs: an on-demand trigger for the deterministic date-observation
extractor, scoped to one document. The review queue (accept/reject
pending observations, verified-facts list, manual fact creation) is
Step 4 -- not built yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.facts.date_extraction import extract_date_observations
from app.db.models import AuditLog, Document

router = APIRouter(tags=["facts"])


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


@router.post("/documents/{document_id}/facts/scan-dates")
def scan_document_for_dates(
    document_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """Run the deterministic date-observation extractor over one document.

    Safe to run repeatedly -- already-observed spans are skipped (see
    extract_date_observations()'s docstring), so re-scanning an
    unchanged document creates nothing new.
    """
    document = _get_document_or_404(db, document_id)
    created = extract_date_observations(db, document.case, document)

    db.add(
        AuditLog(
            case_id=document.case_id,
            event_type="date_scan_triggered",
            entity_type="document",
            entity_id=document.document_id,
            actor=actor,
            details={"observations_created": len(created)},
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"/documents/{document_id}?date_scan={len(created)}", status_code=303
    )
