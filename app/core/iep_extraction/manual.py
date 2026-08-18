"""Manual/assisted structured-entry path for IEP service records
(IEP Consistency Review Step 2).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §3.3: a human selects a span of
a page's already-rendered effective text and fills in the structured
fields for one `iep_records` row by hand -- the identical
citation-creation mechanics
app/core/annotations/service.py::create_highlight() already uses
(same range validation, same "quoted text is always taken directly
from stored extraction, never from what the browser reports" rule).
Nothing in a later comparison step will ever distinguish an
automatically-extracted record from a manually-entered one -- only
`extraction_method` records which one it was, for transparency.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.iep_extraction.records import FieldValue, create_record_with_fields
from app.core.iep_extraction.services import normalize_service_name
from app.core.ocr.text import effective_text
from app.db.models import Case, Citation, Document, DocumentPage, IepRecord

MANUAL_METHOD = "manual"
RECORD_TYPE = "service"


class InvalidManualRecordRangeError(ValueError):
    """Raised for an empty or out-of-bounds manually-selected span."""


def create_manual_service_record(
    db: Session,
    case: Case,
    document: Document,
    page: DocumentPage,
    start_offset: int,
    end_offset: int,
    *,
    service_name: str,
    minutes: float | None,
    frequency_count: float | None,
    frequency_period: str | None,
    location: str | None,
    provider: str | None,
    actor: str,
) -> IepRecord:
    """Create a service `iep_records` row from a human-selected span
    plus hand-entered field values.

    Raises `InvalidManualRecordRangeError` for an empty/out-of-bounds
    span (same guard `create_highlight()` uses) or `ValueError` for an
    empty `service_name`. Works against the page's effective text --
    native or OCR -- exactly like a highlight, so `quoted_text` can
    never drift from the actual stored extraction. Every other field is
    optional: a human may know only the service name and minutes, for
    instance, and add the rest later or leave it unset. Does not
    commit.
    """
    resolved = effective_text(page)
    text = resolved.text or ""
    if not (0 <= start_offset < end_offset <= len(text)):
        raise InvalidManualRecordRangeError(
            f"Selected range [{start_offset}, {end_offset}) is invalid for a "
            f"page with {len(text)} characters of text."
        )
    stripped_name = service_name.strip()
    if not stripped_name:
        raise ValueError("Service name cannot be empty.")
    quoted_text = text[start_offset:end_offset]

    citation = Citation(
        document_id=document.document_id,
        page_id=page.page_id,
        start_offset=start_offset,
        end_offset=end_offset,
        quoted_text=quoted_text,
        text_source=resolved.source,
        source_confidence=resolved.confidence,
    )
    db.add(citation)
    db.flush()  # assigns citation.citation_id

    field_values = [FieldValue("service_name", text_value=stripped_name, citation=citation)]
    if minutes is not None:
        field_values.append(FieldValue("minutes", numeric_value=minutes, citation=citation))
    if frequency_count is not None:
        field_values.append(
            FieldValue("frequency_count", numeric_value=frequency_count, citation=citation)
        )
    if frequency_period is not None and frequency_period.strip():
        field_values.append(
            FieldValue("frequency_period", text_value=frequency_period.strip(), citation=citation)
        )
    if location is not None and location.strip():
        field_values.append(FieldValue("location", text_value=location.strip(), citation=citation))
    if provider is not None and provider.strip():
        field_values.append(FieldValue("provider", text_value=provider.strip(), citation=citation))

    return create_record_with_fields(
        db,
        case,
        document=document,
        record_type_name=RECORD_TYPE,
        section_label=f"Page {page.page_number}",
        comparison_key=normalize_service_name(stripped_name),
        extraction_method=MANUAL_METHOD,
        actor=actor,
        field_values=field_values,
    )
