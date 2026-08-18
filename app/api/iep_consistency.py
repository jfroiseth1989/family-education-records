"""IEP Consistency Review Step 2 routes: the deterministic service-line
extractor and the manual/assisted entry path.

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §3/§8. No comparison/flag
routes exist yet -- that is a later step, deliberately out of scope
here. The scan route mirrors app/api/facts.py::scan_document_for_dates()
exactly (safe to run repeatedly; already-cited spans are skipped, so
re-scanning an unchanged document creates nothing new); the manual-entry
route mirrors app/api/annotations.py::add_highlight() (same
page_id/start_offset/end_offset shape, populated by the same selection
JS already used for highlights -- see document_viewer.html/highlight.js).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.iep_extraction.manual import InvalidManualRecordRangeError, create_manual_service_record
from app.core.iep_extraction.services import extract_service_records
from app.db.models import AuditLog, Case, Document, DocumentPage, IepRecord

router = APIRouter(tags=["iep-consistency"])


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


def _get_page_by_id_or_404(db: Session, page_id: int) -> DocumentPage:
    page = db.get(DocumentPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id} not found.")
    return page


def _get_record_or_404(db: Session, record_id: int) -> IepRecord:
    record = db.get(IepRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Record {record_id} not found.")
    return record


@router.post("/documents/{document_id}/iep-records/scan")
def scan_document_for_services(
    document_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """Run the deterministic service-line extractor over one document.

    Safe to run repeatedly -- already-cited spans are skipped (see
    extract_service_records()'s docstring), so re-scanning an unchanged
    document creates nothing new.
    """
    document = _get_document_or_404(db, document_id)
    case = db.get(Case, document.case_id)
    created = extract_service_records(db, case, document)

    db.add(
        AuditLog(
            case_id=document.case_id,
            event_type="iep_service_scan_triggered",
            entity_type="document",
            entity_id=document.document_id,
            actor=actor,
            details={"records_created": len(created)},
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"/documents/{document_id}?iep_scan_found={len(created)}", status_code=303
    )


@router.post("/documents/{document_id}/iep-records/service")
def add_manual_service_record(
    document_id: int,
    page_id: int = Form(...),
    start_offset: int = Form(...),
    end_offset: int = Form(...),
    service_name: str = Form(...),
    minutes: str = Form(""),
    frequency_count: str = Form(""),
    frequency_period: str = Form(""),
    location: str = Form(""),
    provider: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    document = _get_document_or_404(db, document_id)
    case = db.get(Case, document.case_id)
    page = _get_page_by_id_or_404(db, page_id)
    if page.document_id != document_id:
        raise HTTPException(status_code=400, detail="Page does not belong to this document.")

    try:
        minutes_value = float(minutes.strip()) if minutes.strip() else None
        frequency_count_value = float(frequency_count.strip()) if frequency_count.strip() else None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Minutes and frequency count must be numbers."
        ) from exc

    try:
        record = create_manual_service_record(
            db,
            case,
            document,
            page,
            start_offset,
            end_offset,
            service_name=service_name,
            minutes=minutes_value,
            frequency_count=frequency_count_value,
            frequency_period=frequency_period.strip() or None,
            location=location.strip() or None,
            provider=provider.strip() or None,
            actor=actor,
        )
    except (InvalidManualRecordRangeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.add(
        AuditLog(
            case_id=document.case_id,
            event_type="iep_record_added_manually",
            entity_type="iep_record",
            entity_id=record.record_id,
            actor=actor,
            details={"record_type": "service"},
        )
    )
    db.commit()

    return RedirectResponse(url=f"/documents/{document_id}/view?page={page.page_number}", status_code=303)


@router.post("/iep-records/{record_id}/exclude")
def exclude_iep_record(
    record_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """Mark a record `excluded` -- the one correction a human has over a
    specific extraction without a full edit UI (docs/IEP_CONSISTENCY_
    REVIEW_PLAN.md §2.3). Never deletes the record or its fields; a
    later step's comparison engine simply skips `excluded` records.
    """
    record = _get_record_or_404(db, record_id)
    record.status = "excluded"
    db.add(
        AuditLog(
            case_id=record.case_id,
            event_type="iep_record_excluded",
            entity_type="iep_record",
            entity_id=record.record_id,
            actor=actor,
        )
    )
    db.commit()
    return RedirectResponse(url=f"/documents/{record.document_id}", status_code=303)


@router.post("/iep-records/{record_id}/restore")
def restore_iep_record(
    record_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """Reverse an `exclude` -- sets the record back to `active`."""
    record = _get_record_or_404(db, record_id)
    record.status = "active"
    db.add(
        AuditLog(
            case_id=record.case_id,
            event_type="iep_record_restored",
            entity_type="iep_record",
            entity_id=record.record_id,
            actor=actor,
        )
    )
    db.commit()
    return RedirectResponse(url=f"/documents/{record.document_id}", status_code=303)
