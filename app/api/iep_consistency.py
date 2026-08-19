"""IEP Consistency Review API routes: Step 2's deterministic
service-line extractor and manual/assisted entry path, plus Step 3's
deterministic comparison scan and inconsistency-flag review lifecycle.

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §3/§7/§8. The extraction scan
route mirrors app/api/facts.py::scan_document_for_dates() exactly (safe
to run repeatedly; already-cited spans are skipped, so re-scanning an
unchanged document creates nothing new); the manual-entry route mirrors
app/api/annotations.py::add_highlight() (same page_id/start_offset/
end_offset shape, populated by the same selection JS already used for
highlights -- see document_viewer.html/highlight.js). The Step 3
comparison scan is the same "safe to re-run" shape, idempotent via
`dedup_key` (see app/core/iep_consistency/service.py) rather than via
citation span.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.iep_consistency.service import (
    confirm_flag,
    dismiss_flag,
    list_inconsistency_flags,
    scan_document_for_service_inconsistencies,
    set_flag_note,
)
from app.core.iep_extraction.manual import InvalidManualRecordRangeError, create_manual_service_record
from app.core.iep_extraction.services import extract_service_records
from app.db.models import AuditLog, Case, Document, DocumentPage, IepInconsistencyFlag, IepRecord

router = APIRouter(tags=["iep-consistency"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Student {case_id} not found.")
    return case


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


def _get_flag_or_404(db: Session, case_id: int, flag_id: int) -> IepInconsistencyFlag:
    flag = db.get(IepInconsistencyFlag, flag_id)
    if flag is None or flag.case_id != case_id:
        raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found for this student.")
    return flag


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


# --- Step 3: deterministic comparison + inconsistency-flag review lifecycle -----


@router.post("/cases/{case_id}/consistency/scan")
def scan_case_for_inconsistencies(
    case_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """Run the deterministic within-document service comparison (§4
    rules #1-#2) over every non-deleted document in this case.

    Safe to run repeatedly: each document's comparison is idempotent
    via `dedup_key` (see scan_document_for_service_inconsistencies()'s
    docstring), so re-running finds only genuinely new mismatches --
    never duplicates a pending flag, never resurrects a confirmed or
    dismissed one.
    """
    case = _get_case_or_404(db, case_id)
    documents = list(
        db.scalars(
            select(Document).where(Document.case_id == case_id, Document.deleted_at.is_(None))
        ).all()
    )

    created_count = 0
    for document in documents:
        created = scan_document_for_service_inconsistencies(db, case, document, actor)
        created_count += len(created)

    db.add(
        AuditLog(
            case_id=case_id,
            event_type="iep_inconsistency_scan_triggered",
            entity_type="case",
            entity_id=case_id,
            actor=actor,
            details={"documents_scanned": len(documents), "flags_created": created_count},
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"/cases/{case_id}/consistency?scan_found={created_count}", status_code=303
    )


@router.get("/cases/{case_id}/consistency", response_class=HTMLResponse)
def review_case_consistency(request: Request, case_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the case's inconsistency-flag queue, grouped by status."""
    case = _get_case_or_404(db, case_id)
    flags = list_inconsistency_flags(db, case_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "consistency_review.html",
        {
            "case": case,
            "pending_flags": [f for f in flags if f.status == "pending"],
            "confirmed_flags": [f for f in flags if f.status == "confirmed"],
            "dismissed_flags": [f for f in flags if f.status == "dismissed"],
            "scan_result": request.query_params.get("scan_found"),
        },
    )


@router.post("/cases/{case_id}/consistency/flags/{flag_id}/confirm")
def confirm_inconsistency_flag(
    case_id: int, flag_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """A human reviewed both sources and agrees they're inconsistent.

    Reversible -- a later `dismiss` call simply moves it to
    `dismissed`; neither transition is a one-way door (§7).
    """
    flag = _get_flag_or_404(db, case_id, flag_id)
    confirm_flag(flag, actor)
    db.add(
        AuditLog(
            case_id=case_id,
            event_type="iep_inconsistency_flag_confirmed",
            entity_type="iep_inconsistency_flag",
            entity_id=flag.flag_id,
            actor=actor,
        )
    )
    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/consistency", status_code=303)


@router.post("/cases/{case_id}/consistency/flags/{flag_id}/dismiss")
def dismiss_inconsistency_flag(
    case_id: int, flag_id: int, db: Session = Depends(get_db), actor: str = Depends(get_actor)
) -> RedirectResponse:
    """A human reviewed both sources and decided this isn't a real
    inconsistency -- the "Mark not an inconsistency" mockup action.
    Reversible, same as `confirm` (§7).
    """
    flag = _get_flag_or_404(db, case_id, flag_id)
    dismiss_flag(flag, actor)
    db.add(
        AuditLog(
            case_id=case_id,
            event_type="iep_inconsistency_flag_dismissed",
            entity_type="iep_inconsistency_flag",
            entity_id=flag.flag_id,
            actor=actor,
        )
    )
    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/consistency", status_code=303)


@router.post("/cases/{case_id}/consistency/flags/{flag_id}/note")
def set_inconsistency_flag_note(
    case_id: int,
    flag_id: int,
    note_text: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Attach (or clear) the human's own free-text note on a flag --
    the only free-text field anywhere in this schema (§9). Does not
    change `status`.
    """
    flag = _get_flag_or_404(db, case_id, flag_id)
    set_flag_note(flag, note_text.strip() or None)
    db.add(
        AuditLog(
            case_id=case_id,
            event_type="iep_inconsistency_flag_note_set",
            entity_type="iep_inconsistency_flag",
            entity_id=flag.flag_id,
            actor=actor,
        )
    )
    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/consistency", status_code=303)
