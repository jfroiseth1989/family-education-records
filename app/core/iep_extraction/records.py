"""Shared record/field creation core for IEP Consistency Review (Step 2).

The one place a new `iep_records` row (plus its `iep_record_fields`
rows) is ever created, whether by automatic extraction
(app/core/iep_extraction/services.py) or manual/assisted entry
(app/core/iep_extraction/manual.py) -- both build a list of
`FieldValue` and call `create_record_with_fields()` here, so the
validation and schema-shape rules live in exactly one place rather than
being duplicated per caller. See docs/IEP_CONSISTENCY_REVIEW_PLAN.md
§2.3/§2.5.

Step 2 only ever anchors a record to a `Document` -- Communication-
sourced records (`IepRecord.communication_id`) are a later step, per
the plan's own staged sequence (§12). Nothing here reads or writes any
existing table (`documents`, `citations`, `verified_facts`, etc.);
`Citation` rows referenced here must already exist (created by the
caller, exactly like `create_highlight()` creates its own `Citation`
before attaching an `Annotation` to it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.iep_extraction.lookups import get_field_type, get_record_type
from app.db.models import Case, Citation, Document, IepRecord, IepRecordField


@dataclass(frozen=True)
class FieldValue:
    """One field to attach to a new `iep_records` row.

    Exactly one of `text_value`/`numeric_value`/`date_value` must be
    set, matching the named field type's `value_kind` -- validated in
    `create_record_with_fields()`, not here; this is a plain data
    carrier so callers can build a list of these before opening any
    database work.
    """

    field_type_name: str
    text_value: str | None = None
    numeric_value: float | None = None
    date_value: datetime | None = None
    unit: str | None = None
    citation: Citation | None = None
    confidence: float | None = None


_VALUE_KIND_SLOT = {"text": 0, "number": 1, "date": 2}


def create_record_with_fields(
    db: Session,
    case: Case,
    *,
    document: Document,
    record_type_name: str,
    section_label: str | None,
    comparison_key: str | None,
    extraction_method: str,
    actor: str,
    field_values: list[FieldValue],
) -> IepRecord:
    """Create one `iep_records` row plus its `iep_record_fields` rows.

    Raises `ValueError` if `document.case_id` doesn't match `case`, if
    `record_type_name` or any `field_values[i].field_type_name` is
    unknown, if a field's value doesn't match its field type's
    `value_kind` (exactly one of text_value/numeric_value/date_value
    set, and it must be the one the field type expects), or if a
    field's citation belongs to a different document than `document`.
    Does not commit -- same convention as every other core module in
    this application (e.g. app/core/facts/service.py).
    """
    if document.case_id != case.case_id:
        raise ValueError(
            f"Document {document.document_id} belongs to a different student than this record."
        )

    record_type = get_record_type(db, record_type_name)

    resolved_fields = []
    for field_value in field_values:
        field_type = get_field_type(db, field_value.field_type_name)
        set_values = (
            field_value.text_value is not None,
            field_value.numeric_value is not None,
            field_value.date_value is not None,
        )
        if sum(set_values) != 1:
            raise ValueError(
                f"Field '{field_value.field_type_name}' must set exactly one of "
                "text_value/numeric_value/date_value."
            )
        if not set_values[_VALUE_KIND_SLOT[field_type.value_kind]]:
            raise ValueError(
                f"Field '{field_value.field_type_name}' expects a {field_type.value_kind} value."
            )
        if field_value.citation is not None and field_value.citation.document_id != document.document_id:
            raise ValueError(
                f"Citation {field_value.citation.citation_id} belongs to a different "
                "document than this record."
            )
        resolved_fields.append((field_type, field_value))

    record = IepRecord(
        case_id=case.case_id,
        document_id=document.document_id,
        record_type_id=record_type.type_id,
        section_label=section_label,
        comparison_key=comparison_key,
        extraction_method=extraction_method,
        created_by=actor,
    )
    db.add(record)
    db.flush()  # assigns record.record_id

    for field_type, field_value in resolved_fields:
        db.add(
            IepRecordField(
                record_id=record.record_id,
                field_type_id=field_type.type_id,
                text_value=field_value.text_value,
                numeric_value=field_value.numeric_value,
                date_value=field_value.date_value,
                unit=field_value.unit,
                citation_id=field_value.citation.citation_id if field_value.citation else None,
                extraction_confidence=field_value.confidence,
            )
        )

    return record
