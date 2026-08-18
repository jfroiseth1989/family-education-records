"""Deterministic service-line extractor (IEP Consistency Review Step 2).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §3.1/§3.2 #1. Scans a
document's pages for service lines using
app/core/iep_extraction/patterns.py's fixed regex family -- no model,
no fuzzy/AI inference, no cloud call. Every match becomes one
`iep_records` row (`record_type="service"`) with its own `citations`
row pointing at the exact line matched.

Mirrors app/core/facts/date_extraction.py::extract_date_observations()
exactly: `effective_text()` is the scan source (so a page that needed
OCR is scanned like a natively-extracted one), the scan is idempotent
(an already-cited span never produces a second record on a re-scan,
via the same "does a field already cite this exact span" check
`_observation_exists_for_span()` uses), and it never commits -- the
caller controls the transaction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.iep_extraction.patterns import find_service_line_matches
from app.core.iep_extraction.records import FieldValue, create_record_with_fields
from app.core.ocr.text import effective_text
from app.db.models import Case, Citation, Document, IepRecord, IepRecordField

SYSTEM_ACTOR = "system (iep-service-line-regex-v1)"
METHOD = "iep-service-line-regex-v1"
RECORD_TYPE = "service"


def normalize_service_name(service_name: str) -> str:
    """Deterministic comparison key: lowercased, whitespace-collapsed.

    Never a fuzzy/similarity computation -- see
    docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.3: two service records only
    ever get compared later if this key matches exactly.
    """
    return " ".join(service_name.lower().split())


def _record_exists_for_span(db: Session, page_id: int, start_offset: int, end_offset: int) -> bool:
    """True if some `iep_record_fields` row already cites this exact
    page/span -- the same idempotency check
    `date_extraction.py::_observation_exists_for_span()` uses, applied
    here so re-running the scan on an unchanged document never
    duplicates a record.
    """
    existing = db.scalars(
        select(IepRecordField.field_id)
        .join(Citation, Citation.citation_id == IepRecordField.citation_id)
        .where(
            Citation.page_id == page_id,
            Citation.start_offset == start_offset,
            Citation.end_offset == end_offset,
        )
        .limit(1)
    ).first()
    return existing is not None


def extract_service_records(
    db: Session, case: Case, document: Document, actor: str = SYSTEM_ACTOR
) -> list[IepRecord]:
    """Scan every page of `document` for service lines and record them.

    Returns the newly created records (empty if nothing new was found
    -- either no service-line-shaped text exists, or every match was
    already recorded by a prior scan). Does not commit.
    """
    created: list[IepRecord] = []

    for page in sorted(document.pages, key=lambda p: p.page_number):
        resolved = effective_text(page)
        text = resolved.text or ""
        if not text:
            continue

        for line_match in find_service_line_matches(text):
            if _record_exists_for_span(db, page.page_id, line_match.line_start, line_match.line_end):
                continue

            citation = Citation(
                document_id=document.document_id,
                page_id=page.page_id,
                start_offset=line_match.line_start,
                end_offset=line_match.line_end,
                quoted_text=line_match.line_text,
                text_source=resolved.source,
                source_confidence=resolved.confidence,
            )
            db.add(citation)
            db.flush()  # assigns citation.citation_id

            field_values = [
                FieldValue("service_name", text_value=line_match.service_name, citation=citation),
                FieldValue("minutes", numeric_value=float(line_match.minutes), citation=citation),
                FieldValue(
                    "frequency_count", numeric_value=float(line_match.frequency_count), citation=citation
                ),
                FieldValue("frequency_period", text_value=line_match.frequency_period, citation=citation),
            ]
            if line_match.location:
                field_values.append(FieldValue("location", text_value=line_match.location, citation=citation))
            if line_match.provider:
                field_values.append(FieldValue("provider", text_value=line_match.provider, citation=citation))

            record = create_record_with_fields(
                db,
                case,
                document=document,
                record_type_name=RECORD_TYPE,
                section_label=f"Page {page.page_number}",
                comparison_key=normalize_service_name(line_match.service_name),
                extraction_method=METHOD,
                actor=actor,
                field_values=field_values,
            )
            created.append(record)

    return created
