"""Fact & observation routes (Phase 3.5).

See docs/ARCHITECTURE.md §3.7. Step 3 added the on-demand date-scan
trigger. Step 4 adds the review UI: a case-level page listing pending
observations (accept/reject) and verified facts, plus a route for
asserting a fact directly from an existing citation (reachable from the
document viewer) -- no observation queue involved for that path, since a
human citing a document directly needs no machine suggestion in between.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.facts.date_extraction import extract_date_observations
from app.core.facts.service import (
    CONFIDENCE_LABELS,
    create_verified_fact,
    list_pending_observations,
    list_verified_facts,
    promote_observation,
    reject_observation,
)
from app.db.models import AiObservation, AuditLog, Case, Citation, Document

router = APIRouter(tags=["facts"])


def _parse_form_date(value: str) -> datetime | None:
    """An HTML `<input type="date">` submits "" when left blank or "YYYY-MM-DD"
    when filled in -- normalizes both into what the core module expects.
    """
    stripped = value.strip()
    if not stripped:
        return None
    return datetime.combine(date.fromisoformat(stripped), time.min, tzinfo=timezone.utc)


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


def _get_observation_or_404(db: Session, case_id: int, observation_id: int) -> AiObservation:
    observation = db.get(AiObservation, observation_id)
    if observation is None or observation.case_id != case_id:
        raise HTTPException(status_code=404, detail=f"Observation {observation_id} not found in this case.")
    return observation


def _get_citation_or_404(db: Session, citation_id: int) -> Citation:
    citation = db.get(Citation, citation_id)
    if citation is None:
        raise HTTPException(status_code=404, detail=f"Citation {citation_id} not found.")
    return citation


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


@router.get("/cases/{case_id}/facts", response_class=HTMLResponse)
def review_case_facts(request: Request, case_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the case's pending observation queue and verified-facts list."""
    case = _get_case_or_404(db, case_id)

    pending = list_pending_observations(db, case_id)
    facts = list_verified_facts(db, case_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "facts_review.html",
        {
            "case": case,
            "pending_observations": pending,
            "verified_facts": facts,
            "confidence_labels": sorted(CONFIDENCE_LABELS),
        },
    )


@router.post("/cases/{case_id}/facts/observations/{observation_id}/promote")
def promote_case_observation(
    case_id: int,
    observation_id: int,
    confidence_label: str = Form(...),
    statement: str = Form(""),
    fact_date: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """A human reviews a pending observation and accepts it as a verified fact.

    `fact_date` defaults to blank in the form -- promote_observation()
    then falls back to the observation's own `observed_date`, so a
    reviewer only needs to fill it in to *correct* a date, never to
    duplicate one already captured.
    """
    observation = _get_observation_or_404(db, case_id, observation_id)

    try:
        promote_observation(
            db, observation, confidence_label, actor=actor,
            statement=statement.strip() or None,
            fact_date=_parse_form_date(fact_date),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/facts", status_code=303)


@router.post("/cases/{case_id}/facts/observations/{observation_id}/reject")
def reject_case_observation(
    case_id: int,
    observation_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """A human reviews a pending observation and declines it."""
    observation = _get_observation_or_404(db, case_id, observation_id)

    try:
        reject_observation(db, observation, actor=actor, reason=reason.strip() or None)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/facts", status_code=303)


@router.post("/documents/{document_id}/facts/create-from-citation")
def create_fact_from_citation(
    document_id: int,
    citation_id: int = Form(...),
    fact_type: str = Form(...),
    statement: str = Form(...),
    confidence_label: str = Form(...),
    fact_date: str = Form(""),
    page: int = Form(1),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """A human directly asserts a fact from an existing citation (e.g. a highlight).

    No observation involved -- this is the "assert directly" path
    (docs/ARCHITECTURE.md §3.7), reachable from the document viewer next
    to the citation it's asserted from. `fact_date` is only meaningful
    (and required by create_verified_fact()) when `fact_type` is "date".
    """
    document = _get_document_or_404(db, document_id)
    citation = _get_citation_or_404(db, citation_id)
    if citation.document_id != document_id:
        raise HTTPException(status_code=400, detail="Citation does not belong to this document.")

    try:
        create_verified_fact(
            db, document.case, fact_type, statement, confidence_label,
            [citation_id], actor=actor,
            fact_date=_parse_form_date(fact_date),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/documents/{document_id}/view?page={page}", status_code=303)
