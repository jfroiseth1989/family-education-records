"""Deterministic, local Source suggestion (FERChronos document drop-zone
auto-fill).

No AI/LLM, no cloud classification, no network call of any kind -- same
standing guarantee as app/core/document_type_suggestion.py and
app/core/document_date_suggestion.py. "Source" here means who/where a
document came from (e.g. "North Crawford School District", "emailed by
the district"), not to be confused with `DocumentDateSource` (how a
*date* was determined) elsewhere in this codebase.

Two families of evidence, both requiring a capitalized, letter-only
name phrase (never a lowercase word, never anything containing a digit)
so a match is always a plausible proper name/organization, not an
incidental sentence fragment:

* "<Name> School District" -- the single most common source signal in
  special education records, matched directly.
* A fixed set of attribution anchors ("provided by", "sent by", "issued
  by", "prepared by") immediately followed by a name phrase.

For an email (`is_email=True`), the literal "From: <value>" header line
app/core/extraction/email.py always puts on its own line is also used.

Deliberately conservative, same philosophy as the type and date
suggestion modules: if more than one *distinct* source value is found,
nothing is suggested -- guessing between two different named sources
would be worse than asking the human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME_WORD = r"[A-Z][A-Za-z&'-]*"
_NAME_PHRASE = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,6}}"

_SCHOOL_DISTRICT_PATTERN = re.compile(rf"\b(?P<value>{_NAME_PHRASE}\s+School District)\b")

_ATTRIBUTION_ANCHORS: tuple[str, ...] = ("provided by", "sent by", "issued by", "prepared by")
_ATTRIBUTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    # The anchor phrase itself is matched case-insensitively via the
    # scoped inline `(?i:...)` group -- but NOT the rest of the pattern,
    # so `_NAME_PHRASE`'s `[A-Z]` capitalization requirement (the whole
    # point of requiring a plausible proper name) stays enforced. A bare
    # `re.IGNORECASE` compile flag would silently defeat that requirement
    # for the entire pattern, not just the anchor.
    re.compile(rf"\b(?i:{re.escape(anchor)})\s+(?P<value>{_NAME_PHRASE})\b")
    for anchor in _ATTRIBUTION_ANCHORS
)

_EMAIL_FROM_HEADER = re.compile(r"(?im)^from:\s*(?P<value>[^\n]{1,120})")

_MAX_TEXT_SAMPLE_CHARS = 4000


@dataclass(frozen=True)
class SourceSuggestion:
    value: str
    matched_phrase: str


def _clean(value: str) -> str:
    return value.strip().rstrip(".").strip()


def suggest_document_source(text: str | None, *, is_email: bool = False) -> SourceSuggestion | None:
    """Suggest a Source value from `text`, or None if nothing clearly
    supports one.

    `is_email` enables the email-From:-header path and should only ever
    be True for a genuine `.eml` source, same convention as
    `app.core.document_date_suggestion.suggest_document_dates`.
    """
    if not text:
        return None

    sample = text[:_MAX_TEXT_SAMPLE_CHARS]
    candidates: list[tuple[str, str]] = []

    for m in _SCHOOL_DISTRICT_PATTERN.finditer(sample):
        candidates.append((_clean(m.group("value")), m.group(0).strip()))

    for pattern in _ATTRIBUTION_PATTERNS:
        for m in pattern.finditer(sample):
            candidates.append((_clean(m.group("value")), m.group(0).strip()))

    if is_email:
        m = _EMAIL_FROM_HEADER.search(sample)
        if m:
            candidates.append((_clean(m.group("value")), m.group(0).strip()))

    if not candidates:
        return None

    distinct_values = {value for value, _ in candidates}
    if len(distinct_values) != 1:
        return None

    value, matched_phrase = candidates[0]
    if not value:
        return None
    return SourceSuggestion(value=value, matched_phrase=matched_phrase)
