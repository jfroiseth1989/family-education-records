"""Shared deterministic date-pattern matching (no AI, no network).

Three fixed pattern families -- full/abbreviated month name, ISO 8601,
numeric MM/DD/YYYY (US convention) -- each with a fixed confidence score
tied to how unambiguous that pattern is, never a computed/learned score.
A 2-digit year is intentionally never matched; assuming a century would
be a guess, not a deterministic read of what's on the page.

Extracted from app/core/facts/date_extraction.py (Phase 3.5 Step 3, which
still owns turning a match into an `ai_observations` row) so this exact
matching logic has one definition, shared with
app/core/document_date_suggestion.py (FERChronos document drop-zone
auto-fill), rather than two copies drifting apart over time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

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
class DateMatch:
    start: int
    end: int
    matched_text: str
    parsed_date: date
    confidence_score: float


def valid_date(year: int, month: int, day: int) -> date | None:
    if not (_MIN_YEAR <= year <= _max_year()):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_month_name_matches(text: str) -> list[DateMatch]:
    matches = []
    for m in _MONTH_NAME_PATTERN.finditer(text):
        month_word = m.group("month").lower()
        month = _MONTH_NAMES.get(month_word) or _MONTH_ABBREVIATIONS.get(month_word)
        if month is None:
            continue
        parsed = valid_date(int(m.group("year")), month, int(m.group("day")))
        if parsed is None:
            continue
        matches.append(DateMatch(m.start(), m.end(), m.group(0), parsed, 0.9))
    return matches


def find_iso_matches(text: str) -> list[DateMatch]:
    matches = []
    for m in _ISO_PATTERN.finditer(text):
        parsed = valid_date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        if parsed is None:
            continue
        matches.append(DateMatch(m.start(), m.end(), m.group(0), parsed, 0.85))
    return matches


def find_numeric_matches(text: str) -> list[DateMatch]:
    """MM/DD/YYYY or MM-DD-YYYY, US convention assumed -- lower confidence
    than the other two patterns since the separator alone can't
    disambiguate month-first from day-first, and it's the pattern most
    likely to false-positive on an unrelated number sequence.
    """
    matches = []
    for m in _NUMERIC_PATTERN.finditer(text):
        parsed = valid_date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        if parsed is None:
            continue
        matches.append(DateMatch(m.start(), m.end(), m.group(0), parsed, 0.6))
    return matches


def find_date_matches(text: str) -> list[DateMatch]:
    """All date-like matches in `text`, across all three patterns, with
    overlapping spans resolved by keeping the earliest-starting,
    highest-confidence match (patterns are structurally distinct enough
    -- ISO requires the year first, numeric requires it last -- that a
    true overlap is rare, but this keeps the result well-defined either way).
    """
    all_matches = find_month_name_matches(text) + find_iso_matches(text) + find_numeric_matches(text)
    all_matches.sort(key=lambda dm: (dm.start, -dm.confidence_score))

    kept: list[DateMatch] = []
    last_end = -1
    for dm in all_matches:
        if dm.start < last_end:
            continue
        kept.append(dm)
        last_end = dm.end
    return kept
