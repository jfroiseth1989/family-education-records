"""Tests for app/core/document_notes_suggestion.py -- deterministic,
local Notes summary suggestion (FERChronos document drop-zone auto-fill).
"""

from __future__ import annotations

from pathlib import Path

from app.core.document_date_suggestion import suggest_document_dates
from app.core.document_notes_suggestion import compose_notes_summary
from app.core.document_source_suggestion import suggest_document_source
from app.core.document_type_suggestion import suggest_document_type


def test_composes_full_summary_matching_the_worked_example():
    filename = "IF 22-23 Annual IEP.pdf"
    text = (
        "Individualized Education Program (IEP)\n"
        "Meeting Date: 08/22/2023\n"
        "IEP Implementation Date: 09/01/2023\n"
        "North Crawford School District\n"
    )
    type_suggestion = suggest_document_type(filename, text)
    dates = suggest_document_dates(text)
    source = suggest_document_source(text)

    notes = compose_notes_summary(
        filename=filename,
        text_sample=text,
        type_suggestion=type_suggestion,
        document_date=dates.document_date,
        informational=dates.informational,
        source=source,
    )

    assert notes is not None
    assert notes.text == (
        "Annual IEP. Meeting date: 2023-08-22. "
        "Projected implementation date: 2023-09-01. "
        "North Crawford School District."
    )


def test_no_inputs_returns_none():
    notes = compose_notes_summary(
        filename="random.pdf",
        text_sample=None,
        type_suggestion=None,
        document_date=None,
        informational=(),
        source=None,
    )
    assert notes is None


def test_type_only_produces_one_sentence():
    filename = "letter.pdf"
    text = "This Prior Written Notice explains the proposed change."
    type_suggestion = suggest_document_type(filename, text)

    notes = compose_notes_summary(
        filename=filename,
        text_sample=text,
        type_suggestion=type_suggestion,
        document_date=None,
        informational=(),
        source=None,
    )
    assert notes is not None
    assert notes.text == "Prior Written Notice."


def test_qualifier_not_applied_when_not_adjacent_to_matched_term():
    """"Annual" appearing elsewhere in the text, not immediately before
    the type's own matched term, must never be borrowed as a qualifier.
    """
    filename = "notes.pdf"
    text = "This is our Annual Report of activities. Individualized Education Program details follow."
    type_suggestion = suggest_document_type(filename, text)
    assert type_suggestion is not None
    assert type_suggestion.type_name == "IEP"

    notes = compose_notes_summary(
        filename=filename,
        text_sample=text,
        type_suggestion=type_suggestion,
        document_date=None,
        informational=(),
        source=None,
    )
    assert notes is not None
    assert notes.text == "IEP."


def test_never_contains_opinion_or_legal_conclusion_boilerplate():
    """Static guard: the composer must only ever emit its own fixed
    template labels plus verbatim structured values -- never any of a
    small set of judgment-laden words that would signal an opinion,
    diagnosis, allegation, or legal conclusion sneaking into the
    template itself.
    """
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "document_notes_suggestion.py"
    )
    source = module_path.read_text()
    forbidden_words = ["fault", "blame", "violat", "negligen", "should have", "failed to", "unlawful"]
    lowered = source.lower()
    offenders = [w for w in forbidden_words if w in lowered]
    assert offenders == [], f"Judgment-laden language found in composer module: {offenders}"


def test_no_network_ai_cloud_or_llm_code_in_suggestion_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "document_notes_suggestion.py"
    )
    source = module_path.read_text()
    forbidden_substrings = [
        "requests",
        "httpx",
        "urllib",
        "socket",
        "openai",
        "anthropic",
        "boto3",
        "http://",
        "https://",
    ]
    lowered = source.lower()
    offenders = [s for s in forbidden_substrings if s.lower() in lowered]
    assert offenders == [], f"Forbidden references found in suggestion module: {offenders}"
