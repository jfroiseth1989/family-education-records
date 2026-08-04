"""Tests for app/core/document_type_suggestion.py -- deterministic, local
document-type suggestion (FERChronos Step 5.6).
"""

from __future__ import annotations

from pathlib import Path

from app.core.document_type_suggestion import suggest_document_type


def test_text_iep_full_phrase():
    result = suggest_document_type(
        "notes.pdf", text_sample="The team finalized the Individualized Education Program today."
    )
    assert result is not None
    assert result.type_name == "IEP"
    assert "individualized education program" in result.matched_terms


def test_text_iep_acronym_alone():
    result = suggest_document_type("scan.pdf", text_sample="Please review the attached IEP.")
    assert result is not None
    assert result.type_name == "IEP"
    assert "IEP" in result.matched_terms


def test_filename_transportation_plan():
    result = suggest_document_type("Transportation Plan 2024.pdf")
    assert result is not None
    assert result.type_name == "Transportation Plan"
    assert "transportation plan" in result.matched_terms


def test_filename_alone_drives_suggestion_even_with_unrelated_text():
    result = suggest_document_type(
        "transportation-plan-2024.pdf", text_sample="Just some unrelated body text."
    )
    assert result is not None
    assert result.type_name == "Transportation Plan"


def test_text_functional_behavioral_assessment_full_phrase():
    result = suggest_document_type(
        "notes.pdf", text_sample="This Functional Behavioral Assessment was conducted in March."
    )
    assert result is not None
    assert result.type_name == "Functional Behavioral Assessment (FBA)"
    assert "functional behavioral assessment" in result.matched_terms


def test_text_fba_acronym_alone():
    result = suggest_document_type("notes.pdf", text_sample="See the attached FBA for details.")
    assert result is not None
    assert result.type_name == "Functional Behavioral Assessment (FBA)"
    assert "FBA" in result.matched_terms


def test_text_bip_full_phrase_and_acronym_both_present_is_one_match():
    """Both the full phrase and the acronym appear in the same document --
    that must still be treated as one confident match on one type, not
    ambiguity, and the full phrase should be preferred for display.
    """
    result = suggest_document_type(
        "plan.pdf",
        text_sample="This Behavior Intervention Plan (BIP) addresses classroom behavior.",
    )
    assert result is not None
    assert result.type_name == "Behavior Intervention Plan (BIP)"
    assert result.matched_terms[0] == "behavior intervention plan"


def test_filename_report_card():
    result = suggest_document_type("Report Card Q2.pdf")
    assert result is not None
    assert result.type_name == "Report Card"


def test_text_mediation_agreement():
    result = suggest_document_type(
        "scan001.pdf", text_sample="The parties signed a Mediation Agreement on this date."
    )
    assert result is not None
    assert result.type_name == "Mediation"


def test_text_mediation_request():
    result = suggest_document_type(
        "scan002.pdf", text_sample="This is a formal Mediation Request submitted by the family."
    )
    assert result is not None
    assert result.type_name == "Mediation"


def test_text_prior_written_notice():
    result = suggest_document_type(
        "letter.pdf", text_sample="Please find enclosed the Prior Written Notice regarding placement."
    )
    assert result is not None
    assert result.type_name == "Prior Written Notice"


def test_text_manifestation_determination():
    result = suggest_document_type(
        "review.pdf", text_sample="The team completed a Manifestation Determination review."
    )
    assert result is not None
    assert result.type_name == "Manifestation Determination"


def test_no_match_returns_none():
    result = suggest_document_type("random-file-name.pdf", text_sample="Nothing relevant here.")
    assert result is None


def test_ambiguous_match_across_two_types_returns_none():
    """A document mentioning two distinct triggerable types must produce
    no suggestion at all, per "several categories match, show no
    suggestion" -- never a guess between them.
    """
    result = suggest_document_type(
        "combined.pdf",
        text_sample="This covers both the Mediation Agreement and the Due Process Complaint filed.",
    )
    assert result is None


def test_bare_word_plan_does_not_suggest_transportation_or_bip():
    """Ordinary prose using the generic word "plan" must never be
    misread as Transportation Plan or Behavior Intervention Plan (BIP).
    """
    result = suggest_document_type(
        "notes.pdf", text_sample="Our plan for next semester is still being discussed."
    )
    assert result is None


def test_bare_word_report_does_not_suggest_report_card():
    result = suggest_document_type(
        "notes.pdf", text_sample="This report summarizes the meeting outcomes."
    )
    assert result is None


def test_never_considers_anything_but_filename_and_text_sample():
    """The function signature itself is the guarantee: only filename and
    text_sample are accepted, so a student name or date can never be a
    matching signal -- this test just documents/locks that contract.
    """
    import inspect

    params = list(inspect.signature(suggest_document_type).parameters)
    assert params == ["filename", "text_sample"]


def test_no_network_ai_cloud_or_llm_code_in_suggestion_module():
    """Static guard: the suggestion module's source must never import or
    reference networking, cloud SDKs, or AI/LLM libraries -- matching
    the standing no-AI/no-network guarantee (docs/PRIVACY_SECURITY.md).
    """
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "document_type_suggestion.py"
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
