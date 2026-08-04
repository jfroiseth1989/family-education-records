"""Deterministic, local document-type suggestion (FERChronos Step 5.6).

No AI/LLM, no cloud classification, no external API, no network call of
any kind -- this module is pure Python string/regex matching over a
filename and a local text sample (already-extracted text or local OCR
output; resolving *which* text represents a page is the caller's job via
app/core/ocr/text.py::effective_text(), not this module's -- keeping this
module free of any DB/model dependency so it's trivially unit-testable in
isolation). See docs/PRIVACY_SECURITY.md for the standing no-AI/no-network
guarantee this module must never violate.

A suggestion here is advisory only and is never written to the database
by this module -- app/api/documents.py decides whether/how to surface it,
and a human always makes the final document-type choice (see
set_document_type() there, and the accept/choose-another controls on the
document detail page). Nothing here ever mutates a Document, and a
suggestion is never treated as a verified fact or timeline event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentTypeSuggestion:
    """One explainable suggestion: the type name and the literal terms
    that triggered it, so the UI can show *why* -- never an opaque score.
    """

    type_name: str
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class _Trigger:
    pattern: str  # word-bounded regex fragment
    display: str  # human-readable matched term shown in the UI


def _t(text: str, display: str | None = None) -> _Trigger:
    return _Trigger(pattern=r"\b" + re.escape(text) + r"\b", display=display or text)


# Document type name -> the specific phrases/acronyms that suggest it,
# each a multi-word phrase or a word-bounded acronym -- deliberately
# never a single generic word like "plan" or "report" alone, so ordinary
# prose doesn't false-positive (e.g. a letter that happens to mention
# "our plan for next semester" must never suggest Transportation Plan or
# Behavior Intervention Plan). Every name here must match a seeded
# DocumentType.name exactly -- see app/db/seed.py DEFAULT_DOCUMENT_TYPES.
#
# "OCR Complaint" below refers to a complaint filed with the (federal)
# Office for Civil Rights -- unrelated to this application's own OCR
# (optical character recognition) feature; the trigger phrase always
# requires "complaint" alongside "ocr", so real OCR-review pages never
# match it.
_TYPE_TRIGGERS: tuple[tuple[str, tuple[_Trigger, ...]], ...] = (
    (
        "IEP",
        (
            _t("individualized education program"),
            _t("individualized education plan"),
            _t("iep", display="IEP"),
        ),
    ),
    ("Transportation Plan", (_t("transportation plan"),)),
    (
        "Functional Behavioral Assessment (FBA)",
        (
            _t("functional behavioral assessment"),
            _t("functional behavior assessment"),
            _t("fba", display="FBA"),
        ),
    ),
    (
        "Behavior Intervention Plan (BIP)",
        (_t("behavior intervention plan"), _t("bip", display="BIP")),
    ),
    ("Report Card", (_t("report card"),)),
    (
        "Mediation",
        (
            _t("mediation agreement"),
            _t("mediation request"),
            _t("mediation session"),
            _t("mediation"),
        ),
    ),
    ("Prior Written Notice", (_t("prior written notice"), _t("pwn", display="PWN"))),
    ("Meeting Notice", (_t("meeting notice"), _t("notice of meeting"))),
    ("Consent Form", (_t("consent form"), _t("parental consent"))),
    ("Progress Report", (_t("progress report"),)),
    ("Service Log", (_t("service log"),)),
    (
        "Therapy Record",
        (_t("therapy record"), _t("therapy session note"), _t("therapy note")),
    ),
    ("Manifestation Determination", (_t("manifestation determination"),)),
    (
        "Restraint/Seclusion Record",
        (
            _t("restraint/seclusion"),
            _t("restraint and seclusion"),
            _t("seclusion record"),
            _t("restraint record"),
        ),
    ),
    ("State Complaint", (_t("state complaint"),)),
    ("OCR Complaint", (_t("ocr complaint", display="OCR Complaint"),)),
    (
        "Due Process",
        (
            _t("due process complaint"),
            _t("due process hearing request"),
            _t("due process hearing"),
            _t("due process request"),
        ),
    ),
)

# Purely a performance cap on how much text this scans -- not a privacy
# measure (everything here already runs in-process on data already in
# the local vault, nothing leaves the machine either way).
_MAX_TEXT_SAMPLE_CHARS = 4000


def suggest_document_type(
    filename: str, text_sample: str | None = None
) -> DocumentTypeSuggestion | None:
    """Suggest a document type from `filename` and an optional local text
    sample, or return None if nothing matches or the match is ambiguous.

    Returns None (no suggestion) rather than guessing when: nothing
    matches, or more than one *different* document type's triggers match
    -- see module docstring "several categories match, show no
    suggestion." Never considers anything other than `filename` and
    `text_sample` -- in particular, never a student name or date, since
    neither is ever passed in here.
    """
    # Filenames commonly use hyphens/underscores where prose uses spaces
    # (e.g. "transportation-plan-2024.pdf") -- normalize both to spaces
    # before matching so a phrase trigger still finds them. This never
    # changes matching behavior for text_sample, which already uses
    # normal prose spacing.
    haystack = re.sub(r"[-_]+", " ", filename or "")
    if text_sample:
        haystack = haystack + "\n" + text_sample[:_MAX_TEXT_SAMPLE_CHARS]

    matched_by_type: dict[str, list[str]] = {}
    for type_name, triggers in _TYPE_TRIGGERS:
        hits = [t.display for t in triggers if re.search(t.pattern, haystack, re.IGNORECASE)]
        if hits:
            matched_by_type[type_name] = hits

    if len(matched_by_type) != 1:
        return None

    ((type_name, hits),) = matched_by_type.items()
    # Prefer showing the longest (most specific/full) matched phrase
    # first -- e.g. "behavior intervention plan" ahead of "BIP" if a
    # document happens to contain both.
    ordered_terms = tuple(sorted(set(hits), key=len, reverse=True))
    return DocumentTypeSuggestion(type_name=type_name, matched_terms=ordered_terms)
