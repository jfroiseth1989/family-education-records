"""Timeline routes (Phase 4 Step 3).

See docs/ARCHITECTURE.md §3.8 and docs/PHASE_4_IMPLEMENTATION_PLAN.md §5.
The case-level view, event creation, incremental fact attachment, and
soft-delete. Every event is built exclusively from `verified_facts` --
this router never reads `citations`/`ai_observations`/`document_pages`
directly, and has no code path that generates an event from raw text or
an AI/LLM/cloud service.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db
from app.core.facts.service import list_verified_facts
from app.core.timeline.service import (
    EVENT_DATE_PRECISIONS,
    attach_fact_to_event,
    compute_date_gaps,
    create_timeline_event,
    get_event_facts,
    list_date_source_candidates,
    list_event_types,
    list_timeline_events,
    remove_timeline_event,
)
from app.db.models import Case, TimelineEvent

router = APIRouter(tags=["timeline"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Student {case_id} not found.")
    return case


def _get_event_or_404(db: Session, case_id: int, event_id: int) -> TimelineEvent:
    event = db.get(TimelineEvent, event_id)
    if event is None or event.case_id != case_id:
        raise HTTPException(status_code=404, detail=f"Timeline event {event_id} not found for this student.")
    return event


def _parse_form_date(value: str) -> datetime | None:
    """An HTML `<input type="date">` submits "" when left blank or "YYYY-MM-DD"
    when filled in -- same normalization as app/api/facts.py's helper.
    """
    stripped = value.strip()
    if not stripped:
        return None
    return datetime.combine(date.fromisoformat(stripped), time.min, tzinfo=timezone.utc)


@router.get("/cases/{case_id}/timeline", response_class=HTMLResponse)
def view_case_timeline(
    request: Request,
    case_id: int,
    event_type_id: int | None = None,
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the case's timeline: chronological events, filters, gaps."""
    case = _get_case_or_404(db, case_id)

    try:
        parsed_start = _parse_form_date(start_date)
        parsed_end = _parse_form_date(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    events = list_timeline_events(
        db, case_id, event_type_id=event_type_id, start_date=parsed_start, end_date=parsed_end
    )
    gaps = compute_date_gaps(events)

    event_rows = []
    for event, gap_days in zip(events, gaps):
        date_source_fact, supporting_facts = get_event_facts(db, event.event_id)
        event_rows.append(
            {
                "event": event,
                "gap_days": gap_days,
                "date_source_fact": date_source_fact,
                "supporting_facts": supporting_facts,
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "case": case,
            "event_rows": event_rows,
            "event_types": list_event_types(db),
            "date_source_candidates": list_date_source_candidates(db, case_id),
            "all_facts": list_verified_facts(db, case_id),
            "event_date_precisions": sorted(EVENT_DATE_PRECISIONS),
            "selected_event_type_id": event_type_id,
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@router.post("/cases/{case_id}/timeline")
def create_case_timeline_event(
    case_id: int,
    event_type: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    date_fact_id: int = Form(...),
    additional_fact_ids: list[int] = Form([]),
    event_date_precision: str = Form("exact"),
    event_date_range_end: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """A human creates a timeline event from an existing date-type verified fact."""
    case = _get_case_or_404(db, case_id)

    try:
        create_timeline_event(
            db, case, event_type, title, date_fact_id, actor=actor,
            description=description,
            additional_fact_ids=additional_fact_ids,
            event_date_precision=event_date_precision,
            event_date_range_end=_parse_form_date(event_date_range_end),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/timeline", status_code=303)


@router.post("/cases/{case_id}/timeline/{event_id}/attach-fact")
def attach_fact_to_case_timeline_event(
    case_id: int,
    event_id: int,
    fact_id: int = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Attach one more supporting verified fact to an existing event."""
    event = _get_event_or_404(db, case_id, event_id)

    try:
        attach_fact_to_event(db, event, fact_id, actor=actor)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/timeline", status_code=303)


@router.post("/cases/{case_id}/timeline/{event_id}/delete")
def delete_case_timeline_event(
    case_id: int,
    event_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Soft-delete a timeline event. Idempotent -- see remove_timeline_event()."""
    event = _get_event_or_404(db, case_id, event_id)
    remove_timeline_event(db, event, actor=actor)
    db.commit()
    return RedirectResponse(url=f"/cases/{case_id}/timeline", status_code=303)
