"""Tests for app/core/document_source_suggestion.py -- deterministic,
local Source suggestion (FERChronos document drop-zone auto-fill).
"""

from __future__ import annotations

from pathlib import Path

from app.core.document_source_suggestion import suggest_document_source


def test_no_text_returns_none():
    assert suggest_document_source(None) is None
    assert suggest_document_source("") is None


def test_school_district_pattern():
    result = suggest_document_source("This IEP was developed at North Crawford School District.")
    assert result is not None
    assert result.value == "North Crawford School District"


def test_provided_by_anchor_with_trailing_date_stops_at_name():
    result = suggest_document_source(
        "This document was provided by North Crawford School District on 05/06/2019."
    )
    assert result is not None
    assert result.value == "North Crawford School District"


def test_lowercase_word_after_anchor_is_not_a_name_match():
    """"Sent by mistake" must never be read as a source name -- the word
    right after the anchor has to look like a proper name (capitalized),
    not ordinary lowercase prose.
    """
    result = suggest_document_source("Sent by mistake to the wrong address.")
    assert result is None


def test_conflicting_sources_suggest_nothing():
    result = suggest_document_source(
        "This was provided by North Crawford School District. It was also sent by Central Office."
    )
    assert result is None


def test_agreeing_anchors_still_produce_one_suggestion():
    result = suggest_document_source(
        "Issued by North Crawford School District. Prepared by North Crawford School District."
    )
    assert result is not None
    assert result.value == "North Crawford School District"


def test_email_from_header_used_only_when_is_email_true():
    text = "From: Jane Smith <jane@ncsd.org>\nSubject: IEP records"
    assert suggest_document_source(text, is_email=False) is None

    result = suggest_document_source(text, is_email=True)
    assert result is not None
    assert "Jane Smith" in result.value


def test_ordinary_prose_with_no_anchor_suggests_nothing():
    result = suggest_document_source("The team discussed placement options at length.")
    assert result is None


def test_no_network_ai_cloud_or_llm_code_in_suggestion_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "document_source_suggestion.py"
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
