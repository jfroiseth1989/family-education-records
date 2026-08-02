"""Case management routes: create, list, view, and edit a case.

Server-rendered (Jinja2) rather than a separate JSON API + SPA — see
docs/ARCHITECTURE.md §4 for why Phase 1 favors this over a JS build
pipeline. Every route that changes state also writes an `AuditLog` entry;
see docs/DATA_MODEL.md "audit_log" for why this is a separate table from
the per-document custody ledger.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.db.models import AuditLog, Case, CaseStatus, DocumentType

router = APIRouter(prefix="/cases", tags=["cases"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Student {case_id} not found.")
    return case


@router.get("", response_class=HTMLResponse)
def list_cases(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the case list with a create-case form."""
    cases = db.scalars(select(Case).order_by(Case.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "case_list.html", {"cases": cases}
    )


@router.post("")
def create_case(
    request: Request,
    label: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Create a new case and redirect to its detail page."""
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Student name is required.")

    case = Case(label=label, description=description.strip() or None)
    db.add(case)
    db.flush()  # assigns case.case_id

    db.add(
        AuditLog(
            case_id=case.case_id,
            event_type="case_created",
            entity_type="case",
            entity_id=case.case_id,
            actor=actor,
            details={"label": case.label},
        )
    )
    db.commit()

    return RedirectResponse(url=f"/cases/{case.case_id}", status_code=303)


@router.get("/{case_id}", response_class=HTMLResponse)
def get_case(request: Request, case_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render a case's detail page: metadata, its documents, and version groups."""
    case = _get_case_or_404(db, case_id)
    documents = sorted(case.documents, key=lambda d: d.ingested_at, reverse=True)
    document_types = db.scalars(
        select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)
    ).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "case_detail.html",
        {
            "case": case,
            "documents": documents,
            "case_statuses": [status.value for status in CaseStatus],
            "document_types": document_types,
        },
    )


@router.post("/{case_id}/edit")
def edit_case(
    request: Request,
    case_id: int,
    label: str = Form(...),
    description: str = Form(""),
    status: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Update a case's label, description, and status."""
    case = _get_case_or_404(db, case_id)

    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Student name is required.")
    valid_statuses = {s.value for s in CaseStatus}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status '{status}'.")

    changes = {}
    if case.label != label:
        changes["label"] = {"old": case.label, "new": label}
    if (case.description or "") != description.strip():
        changes["description"] = {"old": case.description, "new": description.strip() or None}
    if case.status != status:
        changes["status"] = {"old": case.status, "new": status}

    case.label = label
    case.description = description.strip() or None
    case.status = status

    if changes:
        db.add(
            AuditLog(
                case_id=case.case_id,
                event_type="case_edited",
                entity_type="case",
                entity_id=case.case_id,
                actor=actor,
                details=changes,
            )
        )
    db.commit()

    return RedirectResponse(url=f"/cases/{case.case_id}", status_code=303)
