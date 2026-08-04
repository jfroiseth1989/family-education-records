"""Deterministic document-date / date-received suggestion (FERChronos
document drop-zone auto-fill).

No AI/LLM, no cloud classification, no network call of any kind -- pure
regex/string matching over a local text sample, built on the same
pattern-matching primitives as app/core/facts/date_extraction.py (see
app/core/date_patterns.py). This module adds *role* awareness those
primitives don't have on their own: a bare date-like substring found
anywhere in a document says nothing about whether it belongs in
"Document Date" or "Date Received" -- what makes that determination here
is a short, fixed list of anchor phrases immediately preceding the date
("Meeting Date:", "provided to the parent on", ...), each phrase
associated with exactly one role. A date with no matching anchor nearby
is never suggested into any field.

Deliberately conservative in the same way
app/core/document_type_suggestion.py is: if more than one *distinct* date
value is found for the same role (e.g. two different "meeting date"
mentions with different values), nothing is suggested for that role --
guessing between them would be worse than asking the human. Two anchors
that happen to agree on the same value are not a conflict; they're
corroboration, and still produce exactly one suggestion.

`Date Received` is intentionally *not* inferrable from a generic
creation-context date (a document's own issue/signed/meeting date must
never be suggested as when the parent received their copy of it) --
its anchor phrases are specifically about a copy being provided/sent/
received, plus (only when the source file is an email, `is_email=True`)
the literal "Date:" header line app/core/extraction/email.py already
puts at the top of an extracted message's text, since that is an
unambiguous "when this archived copy exists" signal for a FERPA-
production email with no phrase-guessing needed.

A small set of "informational" anchors (implementation date, effective
date) are matched but never fill any field -- they're surfaced as plain
text so a reviewer sees them without either being silently absorbed into
the wrong field or lost entirely.

Called from two places: the pre-ingestion preview endpoint (native-text
only, see app/api/documents.py), and, once OCR text becomes available,
the same deferred-suggestion surface on the document detail page that
already offers a document-type suggestion (FERChronos Step 5.6) -- one
function, not two copies, so both surfaces agree on what counts as a
supportable date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.core.date_patterns import DateMatch, find_date_matches

# How far past an anchor phrase (or past a first date, when looking for a
# range's second date) to look for the date it's introducing. Wide enough
# for "Meeting Date: August 22, 2023" or a stray extra word or two, narrow
# enough that an anchor's date doesn't accidentally pick up a *different*
# date mentioned later in an unrelated sentence.
_ANCHOR_WINDOW_CHARS = 40

_DOCUMENT_DATE_ANCHORS: tuple[str, ...] = (
    "meeting date",
    "date of meeting",
    "meeting held on",
    "iep date",
    "date developed",
    "developed on",
    "date revised",
    "revised on",
    "date finalized",
    "finalized on",
    "date signed",
    "signed on",
    "issue date",
    "issued on",
    "date of report",
    "date of evaluation",
    "evaluation date",
    "date of assessment",
    "assessment date",
)

_DATE_RECEIVED_ANCHORS: tuple[str, ...] = (
    "provided to the parent on",
    "provided to parent on",
    "provided to parents on",
    "received by the parent on",
    "received by parent on",
    "received by parents on",
    "sent to the parent on",
    "sent to parent on",
    "mailed to the parent on",
    "mailed to parent on",
    "delivered to the parent on",
    "delivered to parent on",
    "copy provided on",
    "notice provided on",
)

# Only checked when the caller says the source file is an email -- see
# module docstring. Matches the "Date: <value>" header line
# app/core/extraction/email.py always puts on its own line.
_EMAIL_DATE_HEADER_ANCHOR = re.compile(r"(?im)^date:\s*")

# label -> phrases. Matched but never written into document_date or
# date_received -- see module docstring.
_INFORMATIONAL_ANCHORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "implementation date",
        ("iep implementation date", "implementation date", "date of implementation", "implemented on"),
    ),
    ("effective date", ("effective date", "effective as of")),
)

_RANGE_CONNECTOR = re.compile(r"^\s*(?:-|–|—|to|through)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class DateFieldSuggestion:
    """One suggested value for a single date-shaped form field."""

    value: date
    range_end: date | None
    precision: str  # "exact" | "range"
    matched_phrase: str  # verbatim source text, e.g. "Meeting Date: 08/22/2023"


@dataclass(frozen=True)
class InformationalDateNote:
    """A date found via an anchor that's deliberately never auto-filled
    into a field -- shown to the reviewer as context only.
    """

    label: str
    value: date
    matched_phrase: str


@dataclass(frozen=True)
class DocumentDateSuggestions:
    document_date: DateFieldSuggestion | None
    date_received: DateFieldSuggestion | None
    informational: tuple[InformationalDateNote, ...]


@dataclass(frozen=True)
class _AnchoredMatch:
    matched_phrase: str
    start_offset: int  # where matched_phrase starts in the original text
    end_offset: int  # where matched_phrase ends in the original text
    date_match: DateMatch


def _find_anchored_matches(text: str, anchor_pattern: re.Pattern[str]) -> list[_AnchoredMatch]:
    """Every occurrence of `anchor_pattern` in `text`, paired with the
    *nearest* date-like substring within the following
    `_ANCHOR_WINDOW_CHARS` -- an anchor occurrence with no date at all
    nearby (a form label with a blank line after it) contributes nothing.

    Deliberately takes the nearest date, not "the only one nearby": a
    range ("Meeting held on March 1, 2024 - March 15, 2024") legitimately
    has a second date immediately after the first, and
    `_extend_into_range()` is what decides whether that second date
    extends the suggestion or is unrelated. The real safety net against
    actually conflicting information is `_resolve_single_role()`, which
    still refuses to suggest anything for a role when different anchor
    occurrences (or different phrasings of the same role) point at
    different values.
    """
    results: list[_AnchoredMatch] = []
    for m in anchor_pattern.finditer(text):
        window_start = m.end()
        window = text[window_start : window_start + _ANCHOR_WINDOW_CHARS]
        candidates = find_date_matches(window)
        if not candidates:
            continue
        dm = candidates[0]  # nearest date to the anchor
        end_offset = window_start + dm.end
        phrase = text[m.start() : end_offset].strip()
        results.append(
            _AnchoredMatch(matched_phrase=phrase, start_offset=m.start(), end_offset=end_offset, date_match=dm)
        )
    return results


def _find_anchored_matches_for_phrase(text: str, phrase: str) -> list[_AnchoredMatch]:
    return _find_anchored_matches(text, re.compile(re.escape(phrase), re.IGNORECASE))


def _extend_into_range(text: str, match: _AnchoredMatch) -> DateFieldSuggestion:
    """If a range connector ("-", "through", ...) immediately follows
    `match`'s date, and exactly one more date follows *that*, extend it
    into a range suggestion. Anything less clear-cut (no connector, or
    more than one trailing date) stays a plain exact-date suggestion --
    Range End is only ever filled when the document clearly reads as a
    range.
    """
    window = text[match.end_offset : match.end_offset + _ANCHOR_WINDOW_CHARS]
    connector = _RANGE_CONNECTOR.match(window)
    if connector is not None:
        remainder = window[connector.end() :]
        candidates = find_date_matches(remainder)
        if len(candidates) == 1:
            range_end_match = candidates[0]
            full_phrase_end = match.end_offset + connector.end() + range_end_match.end
            full_phrase = text[match.start_offset : full_phrase_end].strip()
            return DateFieldSuggestion(
                value=match.date_match.parsed_date,
                range_end=range_end_match.parsed_date,
                precision="range",
                matched_phrase=full_phrase,
            )

    return DateFieldSuggestion(
        value=match.date_match.parsed_date,
        range_end=None,
        precision="exact",
        matched_phrase=match.matched_phrase,
    )


def _resolve_single_role(matches: list[_AnchoredMatch], text: str) -> DateFieldSuggestion | None:
    """Combine every anchored match found for one role into a single
    suggestion, or None if they disagree (see module docstring).
    """
    if not matches:
        return None

    distinct_values = {m.date_match.parsed_date for m in matches}
    if len(distinct_values) != 1:
        return None

    return _extend_into_range(text, matches[0])


def suggest_document_dates(text: str | None, *, is_email: bool = False) -> DocumentDateSuggestions:
    """Suggest Document Date / Date Received values (and any purely
    informational dates) from `text`, or an all-empty result if nothing
    in it clearly supports a suggestion.

    `text` is whatever local text is available at call time -- native
    extraction pre-ingestion, or effective (native-or-OCR) text once OCR
    has run; this function has no opinion on where it came from. `is_email`
    enables the email-Date:-header path for Date Received (see module
    docstring) and should only ever be True for a genuine `.eml` source.
    """
    if not text:
        return DocumentDateSuggestions(document_date=None, date_received=None, informational=())

    document_date_matches = [
        m
        for anchor in _DOCUMENT_DATE_ANCHORS
        for m in _find_anchored_matches_for_phrase(text, anchor)
    ]
    document_date = _resolve_single_role(document_date_matches, text)

    date_received_matches = [
        m
        for anchor in _DATE_RECEIVED_ANCHORS
        for m in _find_anchored_matches_for_phrase(text, anchor)
    ]
    if is_email:
        date_received_matches += _find_anchored_matches(text, _EMAIL_DATE_HEADER_ANCHOR)
    date_received = _resolve_single_role(date_received_matches, text)

    informational: list[InformationalDateNote] = []
    seen_values: set[tuple[str, date]] = set()
    for label, phrases in _INFORMATIONAL_ANCHORS:
        for phrase in phrases:
            for m in _find_anchored_matches_for_phrase(text, phrase):
                key = (label, m.date_match.parsed_date)
                if key in seen_values:
                    continue
                seen_values.add(key)
                informational.append(
                    InformationalDateNote(
                        label=label, value=m.date_match.parsed_date, matched_phrase=m.matched_phrase
                    )
                )

    return DocumentDateSuggestions(
        document_date=document_date,
        date_received=date_received,
        informational=tuple(informational),
    )
