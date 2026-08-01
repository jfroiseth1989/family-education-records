"""Deterministic date-observation extractor (Phase 3.5 Step 3).

See docs/PROJECT_PLAN.md decision #11 and docs/PHASE_3_DECISIONS.md.
Scans a document's pages for date-like substrings using fixed regular
expressions and calendar validation only -- no model, no fuzzy/AI
inference, no cloud call. Every match becomes an `ai_observations` row
(via app/core/facts/service.py::create_ai_observation()) with its own new
`citations` row pointing at the exact page/offset span matched, so a
human reviewing the observation can always see precisely what text
produced it. `effective_text()` (app/core/ocr/text.py) is used as the
scan source, so a page that needed OCR is scanned exactly like a
natively-extracted one, once Phase 3 has populated it.

This is deliberately narrow, not a general date parser: three fixed
pattern families (full/abbreviated month name, ISO 8601, numeric
MM/DD/YYYY assuming US convention), each with a fixed confidence score
tied to how unambiguous that pattern is -- never a computed/learned
score. A 2-digit year is intentionally never matched; assuming a century
would be a guess, not a deterministic read of what's on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.facts.service import create_ai_observation
from app.core.ocr.text import effective_text
from app.db.models import AiObservation, AiObservationCitation, Case, Citation, Document

SYSTEM_ACTOR = "system (regex-date-parse-v1)"
METHOD = "regex-date-parse-v1"
FACT_TYPE = "date"

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_ABBREVIATIONS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# A century-and-a-bit window -- wide enough to cover any real education
# record, narrow enough to reject a numeric match that's actually
# something else (a case number, a page count) that happens to fall in
# the 4-digit-year slot of the numeric pattern below.
_MIN_YEAR = 1900


def _max_year() -> int:
    return datetime.now(timezone.utc).year + 2


_MONTH_NAME_PATTERN = re.compile(
    r"\b(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b"
)
_ISO_PATTERN = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_NUMERIC_PATTERN = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{4})\b")


@dataclass(frozen=True)
class _DateMatch:
    start: int
    end: int
    matched_text: str
    parsed_date: date
    confidence_score: float


def _valid_date(year: int, month: int, day: int) -> date | None:
    if not (_MIN_YEAR <= year <= _max_year()):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _find_month_name_matches(text: str) -> list[_DateMatch]:
    matches = []
    for m in _MONTH_NAME_PATTERN.finditer(text):
        month_word = m.group("month").lower()
        month = _MONTH_NAMES.get(month_word) or _MONTH_ABBREVIATIONS.get(month_word)
        if month is None:
            continue
        parsed = _valid_date(int(m.group("year")), month, int(m.group("day")))
        if parsed is None:
            continue
        matches.append(_DateMatch(m.start(), m.end(), m.group(0), parsed, 0.9))
    return matches


def _find_iso_matches(text: str) -> list[_DateMatch]:
    matches = []
    for m in _ISO_PATTERN.finditer(text):
        parsed = _valid_date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        if parsed is None:
            continue
        matches.append(_DateMatch(m.start(), m.end(), m.group(0), parsed, 0.85))
    return matches


def _find_numeric_matches(text: str) -> list[_DateMatch]:
    """MM/DD/YYYY or MM-DD-YYYY, US convention assumed -- lower confidence
    than the other two patterns since the separator alone can't
    disambiguate month-first from day-first, and it's the pattern most
    likely to false-positive on an unrelated number sequence.
    """
    matches = []
    for m in _NUMERIC_PATTERN.finditer(text):
        parsed = _valid_date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        if parsed is None:
            continue
        matches.append(_DateMatch(m.start(), m.end(), m.group(0), parsed, 0.6))
    return matches


def _find_date_matches(text: str) -> list[_DateMatch]:
    """All date-like matches in `text`, across all three patterns, with
    overlapping spans resolved by keeping the earliest-starting,
    highest-confidence match (patterns are structurally distinct enough
    -- ISO requires the year first, numeric requires it last -- that a
    true overlap is rare, but this keeps the result well-defined either way).
    """
    all_matches = (
        _find_month_name_matches(text) + _find_iso_matches(text) + _find_numeric_matches(text)
    )
    all_matches.sort(key=lambda dm: (dm.start, -dm.confidence_score))

    kept: list[_DateMatch] = []
    last_end = -1
    for dm in all_matches:
        if dm.start < last_end:
            continue
        kept.append(dm)
        last_end = dm.end
    return kept


def _observation_exists_for_span(db: Session, page_id: int, start_offset: int, end_offset: int) -> bool:
    """True if some observation already cites this exact page/span.

    Makes re-running the scan on a document safe: unchanged text never
    produces duplicate pending-review rows for a human to look at twice.
    A page whose text changed (e.g. after an OCR correction) gets new
    offsets for its matches, so this only ever suppresses true repeats.
    """
    existing = db.scalars(
        select(AiObservationCitation.observation_id)
        .join(Citation, Citation.citation_id == AiObservationCitation.citation_id)
        .where(
            Citation.page_id == page_id,
            Citation.start_offset == start_offset,
            Citation.end_offset == end_offset,
        )
        .limit(1)
    ).first()
    return existing is not None


def extract_date_observations(db: Session, case: Case, document: Document, actor: str = SYSTEM_ACTOR) -> list[AiObservation]:
    """Scan every page of `document` for date-like text and record observations.

    Returns the newly created observations (empty if nothing new was
    found -- either no date-like text exists, or every match was already
    observed by a prior scan). Does not commit -- same convention as
    every other core module; the caller controls the transaction boundary.
    """
    created: list[AiObservation] = []

    for page in sorted(document.pages, key=lambda p: p.page_number):
        resolved = effective_text(page)
        text = resolved.text or ""
        if not text:
            continue

        for dm in _find_date_matches(text):
            if _observation_exists_for_span(db, page.page_id, dm.start, dm.end):
                continue

            citation = Citation(
                document_id=document.document_id,
                page_id=page.page_id,
                start_offset=dm.start,
                end_offset=dm.end,
                quoted_text=dm.matched_text,
                text_source=resolved.source,
                source_confidence=resolved.confidence,
            )
            db.add(citation)
            db.flush()  # assigns citation.citation_id

            observation = create_ai_observation(
                db,
                case,
                FACT_TYPE,
                statement=f"Possible date: {dm.parsed_date.isoformat()}",
                confidence_score=dm.confidence_score,
                method=METHOD,
                citation_ids=[citation.citation_id],
                actor=actor,
            )
            created.append(observation)

    return created
