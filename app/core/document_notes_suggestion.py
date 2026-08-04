"""Deterministic, local Notes summary suggestion (FERChronos document
drop-zone auto-fill).

Composes a short, factual Notes summary purely by assembling pieces this
application already has strong, explainable local evidence for --
document type (app/core/document_type_suggestion.py), document date and
informational dates (app/core/document_date_suggestion.py), and source
(app/core/document_source_suggestion.py) -- never by generating free
text from the document's prose. A "smart" free-text summarizer risks
paraphrasing into an opinion, a diagnosis, an allegation, or a legal
conclusion, none of which belong in an auto-filled Notes field; every
sentence produced here is a fixed template filled in with a value that
already passed one of those modules' own strict, explainable matching
rules, so it can never say more than the source text itself supports.

Returns None (a blank Notes suggestion) when none of the three inputs
produced anything -- an empty Notes field is always a valid, honest
outcome, never something to force text into.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.document_date_suggestion import DateFieldSuggestion, InformationalDateNote
from app.core.document_source_suggestion import SourceSuggestion
from app.core.document_type_suggestion import DocumentTypeSuggestion

# Adjacent scheduling/administrative qualifiers commonly used just before
# a document type name (e.g. "Annual IEP", "Initial Evaluation") --
# deliberately a short, fixed list, never an opinion word.
_TYPE_QUALIFIERS: tuple[str, ...] = (
    "Annual",
    "Initial",
    "Triennial",
    "Amended",
    "Amendment",
    "Draft",
    "Final",
)

# Substring (lowercased) found in a document-date suggestion's
# matched_phrase -> the label to show in Notes. Checked in order; the
# first match wins. Mirrors the anchor phrases in
# app/core/document_date_suggestion.py, but only needs to distinguish
# what to *call* the date here, not to find it.
_DOCUMENT_DATE_LABELS: tuple[tuple[str, str], ...] = (
    ("meeting", "Meeting date"),
    ("iep date", "IEP date"),
    ("developed", "Date developed"),
    ("revised", "Revised date"),
    ("finalized", "Finalized date"),
    ("signed", "Signed date"),
    ("issue", "Issue date"),
    ("evaluation", "Evaluation date"),
    ("assessment", "Assessment date"),
    ("report", "Report date"),
)

_INFORMATIONAL_LABELS: dict[str, str] = {
    "implementation date": "Projected implementation date",
    "effective date": "Effective date",
}


@dataclass(frozen=True)
class NotesSuggestion:
    text: str


def _document_date_label(matched_phrase: str) -> str:
    lowered = matched_phrase.lower()
    for keyword, label in _DOCUMENT_DATE_LABELS:
        if keyword in lowered:
            return label
    return "Document date"


def _qualified_type_label(type_name: str, matched_terms: tuple[str, ...], haystack: str) -> str:
    """`type_name`, prefixed with a scheduling qualifier (e.g. "Annual")
    if -- and only if -- that exact qualifier immediately precedes one of
    the type's own matched terms somewhere in `haystack`. Never a guess:
    the qualifier word itself must be physically adjacent to the
    evidence that already justified the type suggestion.
    """
    for qualifier in _TYPE_QUALIFIERS:
        for term in matched_terms:
            pattern = re.compile(rf"\b{re.escape(qualifier)}\s+{re.escape(term)}\b", re.IGNORECASE)
            if pattern.search(haystack):
                return f"{qualifier} {type_name}"
    return type_name


def compose_notes_summary(
    *,
    filename: str,
    text_sample: str | None,
    type_suggestion: DocumentTypeSuggestion | None,
    document_date: DateFieldSuggestion | None,
    informational: tuple[InformationalDateNote, ...] = (),
    source: SourceSuggestion | None = None,
) -> NotesSuggestion | None:
    """Compose a brief, factual Notes summary from already-suggested
    fields, or None if none of them produced anything to summarize.
    """
    sentences: list[str] = []

    if type_suggestion is not None:
        haystack = f"{filename}\n{text_sample or ''}"
        label = _qualified_type_label(
            type_suggestion.type_name, type_suggestion.matched_terms, haystack
        )
        sentences.append(f"{label}.")

    if document_date is not None:
        label = _document_date_label(document_date.matched_phrase)
        sentences.append(f"{label}: {document_date.value.isoformat()}.")

    for note in informational:
        label = _INFORMATIONAL_LABELS.get(note.label, note.label.capitalize())
        sentences.append(f"{label}: {note.value.isoformat()}.")

    if source is not None:
        sentences.append(f"{source.value}.")

    if not sentences:
        return None

    return NotesSuggestion(text=" ".join(sentences))
