"""Tests for app/core/document_date_suggestion.py -- deterministic,
local Document Date / Date Received suggestion (FERChronos document
drop-zone auto-fill).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.core.document_date_suggestion import suggest_document_dates


def test_no_text_returns_all_empty():
    result = suggest_document_dates(None)
    assert result.document_date is None
    assert result.date_received is None
    assert result.informational == ()

    result = suggest_document_dates("")
    assert result.document_date is None
    assert result.date_received is None


def test_meeting_date_anchor_suggests_document_date():
    result = suggest_document_dates("IEP Team\nMeeting Date: 08/22/2023\nAttendees: ...")
    assert result.document_date is not None
    assert result.document_date.value == date(2023, 8, 22)
    assert result.document_date.precision == "exact"
    assert "Meeting Date" in result.document_date.matched_phrase
    assert result.date_received is None


def test_provided_to_parent_anchor_suggests_date_received():
    result = suggest_document_dates(
        "This Prior Written Notice was provided to the parent on 05/06/2019 by mail."
    )
    assert result.date_received is not None
    assert result.date_received.value == date(2019, 5, 6)
    assert "provided to the parent on" in result.date_received.matched_phrase.lower()
    assert result.document_date is None


def test_bare_issue_date_never_fills_date_received():
    """A document's own issue/creation-context date must never be
    inferred as when the parent received their copy -- Date Received
    requires its own dedicated anchor phrase (or, for an email, the
    Date: header) per the module's non-inferability guarantee.
    """
    result = suggest_document_dates("Issue date: 03/01/2022. This report covers the evaluation.")
    assert result.document_date is not None
    assert result.document_date.value == date(2022, 3, 1)
    assert result.date_received is None


def test_implementation_date_is_informational_only_not_document_date():
    result = suggest_document_dates(
        "IEP Implementation Date: 09/01/2023. Meeting Date: 08/22/2023."
    )
    assert result.document_date is not None
    assert result.document_date.value == date(2023, 8, 22)
    labels = {note.label: note.value for note in result.informational}
    assert labels["implementation date"] == date(2023, 9, 1)


def test_date_range_fills_range_end():
    result = suggest_document_dates(
        "Meeting held on March 1, 2024 - March 15, 2024 for the annual review."
    )
    assert result.document_date is not None
    assert result.document_date.value == date(2024, 3, 1)
    assert result.document_date.range_end == date(2024, 3, 15)
    assert result.document_date.precision == "range"


def test_single_date_after_anchor_is_not_treated_as_range():
    result = suggest_document_dates("Meeting Date: 08/22/2023. The team discussed placement.")
    assert result.document_date is not None
    assert result.document_date.range_end is None
    assert result.document_date.precision == "exact"


def test_conflicting_meeting_dates_suggest_nothing():
    """Two different anchor occurrences for the same role that disagree
    on the value must never be resolved by guessing -- see
    _resolve_single_role's "distinct values" ambiguity guard.
    """
    result = suggest_document_dates(
        "Meeting Date: 08/22/2023. Later, the Meeting Date: 09/01/2023 was rescheduled."
    )
    assert result.document_date is None


def test_agreeing_anchors_still_produce_one_suggestion():
    result = suggest_document_dates(
        "Meeting Date: 08/22/2023. Confirmed: date of meeting 08/22/2023."
    )
    assert result.document_date is not None
    assert result.document_date.value == date(2023, 8, 22)


def test_email_date_header_used_only_when_is_email_true():
    text = "Date: May 6, 2019 10:00:00 -0400\nFrom: district@example.org\nSubject: Records"
    without_email_flag = suggest_document_dates(text, is_email=False)
    assert without_email_flag.date_received is None

    with_email_flag = suggest_document_dates(text, is_email=True)
    assert with_email_flag.date_received is not None
    assert with_email_flag.date_received.value == date(2019, 5, 6)


def test_anchor_with_no_nearby_date_contributes_nothing():
    result = suggest_document_dates("Meeting Date: to be determined. No date set yet.")
    assert result.document_date is None


def test_no_network_ai_cloud_or_llm_code_in_suggestion_module():
    """Static guard: this module must never import or reference
    networking, cloud SDKs, or AI/LLM libraries -- matching the standing
    no-AI/no-network guarantee (docs/PRIVACY_SECURITY.md).
    """
    module_path = (
        Path(__file__).resolve().parents[1] / "app" / "core" / "document_date_suggestion.py"
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
