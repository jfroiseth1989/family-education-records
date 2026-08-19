"""Deterministic within-document comparison rules for service-type
`iep_records` (IEP Consistency Review Step 3).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §4, rules #1-#2 (the
`service_schedule_mismatch` family and `service_location_mismatch` /
`service_provider_mismatch`), scoped to the one structured record type
Step 2 built. All comparisons here are exact, never fuzzy: two values
either match after normalization or they don't -- no similarity score,
no numeric tolerance, no judgment about whether a difference is
"meaningful." That judgment belongs to the human reviewing the flag
("Mark not an inconsistency"), never to this module.

Pure functions only -- nothing here touches the database. Given
already-loaded `IepRecord` rows (with `.fields`, `.fields[*].field_type`,
and `.fields[*].citation` accessible), `compare_service_records_within_document()`
returns a list of `FlagCandidate` describing what a caller (see
app/core/iep_consistency/service.py) should turn into
`iep_inconsistency_flags` rows.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from app.db.models import Citation, IepRecord, IepRecordField

_PUNCTUATION_RE = re.compile(r"[^\w\s]")

# Field-name groups compared as one combined dimension (frequency is
# count+period together -- differing in either means the stated
# frequency doesn't match, one flag, not two).
_SINGLE_FIELD_DIMENSIONS = ("minutes", "location", "provider")


@dataclass(frozen=True)
class FlagCandidate:
    """One deterministic mismatch found between two `iep_records`,
    ready to become an `iep_inconsistency_flags` row. Carries its own
    snapshot of what was compared (`extracted_value_a`/`_b`) so the
    flag remains meaningful even if a source record is later edited or
    excluded -- see docs/IEP_CONSISTENCY_REVIEW_PLAN.md §2.4.
    """

    inconsistency_type_name: str
    rule_id: str
    comparison_mode: str
    source_a_record_id: int
    source_a_field_id: int | None
    source_b_record_id: int
    source_b_field_id: int | None
    reason_text: str
    extracted_value_a: dict
    extracted_value_b: dict


def _normalize_text(value: str | None) -> str | None:
    """Lowercase, punctuation-stripped, whitespace-collapsed -- the
    `text` comparison primitive from §4. Used for value comparison
    only (not `comparison_key` matching, which already happened in
    app/core/iep_extraction/services.py::normalize_service_name()).
    """
    if value is None:
        return None
    stripped = _PUNCTUATION_RE.sub("", value.lower())
    return " ".join(stripped.split())


def _field(record: IepRecord, field_type_name: str) -> IepRecordField | None:
    return next((f for f in record.fields if f.field_type.name == field_type_name), None)


def _field_value(field: IepRecordField) -> str | float | None:
    if field.numeric_value is not None:
        return field.numeric_value
    if field.text_value is not None:
        return field.text_value
    if field.date_value is not None:
        value = field.date_value
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    return None


def _primary_citation(record: IepRecord) -> Citation | None:
    """The citation shared by this record's fields -- both extractors
    in app/core/iep_extraction/ attach one citation per matched
    line/selection to every field on the record, so any field's
    citation is representative of the whole record.
    """
    for field in record.fields:
        if field.citation is not None:
            return field.citation
    return None


def _record_snapshot(record: IepRecord, field_names: tuple[str, ...]) -> dict:
    """A snapshot of exactly what was compared, independent of what
    the live `iep_record_fields` rows might show later (§2.4) --
    the JSON stored as `extracted_value_a`/`extracted_value_b`.
    """
    values: dict[str, str | float | None] = {}
    for name in field_names:
        field = _field(record, name)
        if field is not None:
            values[name] = _field_value(field)

    citation = _primary_citation(record)
    return {
        "record_id": record.record_id,
        "document_id": record.document_id,
        "section_label": record.section_label,
        "extraction_method": record.extraction_method,
        "values": values,
        "quoted_text": citation.quoted_text if citation else None,
        "page_id": citation.page_id if citation else None,
    }


def _build_candidate(
    a: IepRecord,
    b: IepRecord,
    field_a: IepRecordField | None,
    field_b: IepRecordField | None,
    *,
    inconsistency_type_name: str,
    rule_id: str,
    reason_text: str,
    field_names: tuple[str, ...],
) -> FlagCandidate:
    return FlagCandidate(
        inconsistency_type_name=inconsistency_type_name,
        rule_id=rule_id,
        comparison_mode="within_document",
        source_a_record_id=a.record_id,
        source_a_field_id=field_a.field_id if field_a is not None else None,
        source_b_record_id=b.record_id,
        source_b_field_id=field_b.field_id if field_b is not None else None,
        reason_text=reason_text,
        extracted_value_a=_record_snapshot(a, field_names),
        extracted_value_b=_record_snapshot(b, field_names),
    )


def _compare_service_pair(a: IepRecord, b: IepRecord) -> list[FlagCandidate]:
    """Compare two service records that share a `comparison_key`
    (same normalized service name) within the same document.

    Only fires when *both* sides have a value for the dimension being
    compared -- a missing value on either side is never treated as a
    mismatch (that would be guessing from absence, not comparing two
    actual stated values). `a.record_id` is assumed < `b.record_id`
    (the caller orders pairs), so source A/B assignment is stable and
    deterministic across re-runs.
    """
    candidates: list[FlagCandidate] = []

    minutes_a, minutes_b = _field(a, "minutes"), _field(b, "minutes")
    if (
        minutes_a is not None
        and minutes_b is not None
        and minutes_a.numeric_value != minutes_b.numeric_value
    ):
        candidates.append(
            _build_candidate(
                a,
                b,
                minutes_a,
                minutes_b,
                inconsistency_type_name="service_minutes_mismatch",
                rule_id="service_schedule_mismatch_v1",
                reason_text="The stated minutes for this service do not match between these two sources.",
                field_names=("service_name", "minutes"),
            )
        )

    count_a, count_b = _field(a, "frequency_count"), _field(b, "frequency_count")
    period_a, period_b = _field(a, "frequency_period"), _field(b, "frequency_period")
    frequency_differs = False
    if count_a is not None and count_b is not None and count_a.numeric_value != count_b.numeric_value:
        frequency_differs = True
    if (
        period_a is not None
        and period_b is not None
        and _normalize_text(period_a.text_value) != _normalize_text(period_b.text_value)
    ):
        frequency_differs = True
    if frequency_differs:
        candidates.append(
            _build_candidate(
                a,
                b,
                None,
                None,
                inconsistency_type_name="service_frequency_mismatch",
                rule_id="service_schedule_mismatch_v1",
                reason_text="The stated frequency for this service does not match between these two sources.",
                field_names=("service_name", "frequency_count", "frequency_period"),
            )
        )

    for field_name, inconsistency_type_name, rule_id, reason_text in (
        (
            "location",
            "service_location_mismatch",
            "service_location_mismatch_v1",
            "The stated location for this service does not match between these two sources.",
        ),
        (
            "provider",
            "service_provider_mismatch",
            "service_provider_mismatch_v1",
            "The stated provider for this service does not match between these two sources.",
        ),
    ):
        field_a, field_b = _field(a, field_name), _field(b, field_name)
        if (
            field_a is not None
            and field_b is not None
            and _normalize_text(field_a.text_value) != _normalize_text(field_b.text_value)
        ):
            candidates.append(
                _build_candidate(
                    a,
                    b,
                    field_a,
                    field_b,
                    inconsistency_type_name=inconsistency_type_name,
                    rule_id=rule_id,
                    reason_text=reason_text,
                    field_names=("service_name", field_name),
                )
            )

    return candidates


def compare_service_records_within_document(records: list[IepRecord]) -> list[FlagCandidate]:
    """Compare every pair of same-document, same-`comparison_key`
    service records for a differing minutes/frequency/location/
    provider value.

    `records` should already be filtered to `status == "active"`,
    `record_type == "service"`, and a single document by the caller
    (see app/core/iep_consistency/service.py) -- this function doesn't
    re-check either, so it stays a pure comparison over whatever it's
    handed. Records with no `comparison_key` are skipped (nothing to
    group them by). Pairs are ordered by ascending `record_id` so
    source A/B assignment -- and therefore `dedup_key` -- is stable
    across re-runs.
    """
    groups: dict[str, list[IepRecord]] = defaultdict(list)
    for record in records:
        if record.comparison_key:
            groups[record.comparison_key].append(record)

    candidates: list[FlagCandidate] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda r: r.record_id)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                candidates.extend(_compare_service_pair(ordered[i], ordered[j]))
    return candidates
