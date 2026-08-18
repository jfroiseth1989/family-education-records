"""Tests for app/core/iep_extraction/patterns.py (IEP Consistency Review
Step 2) -- pure pattern-matching, no database involved.

Mirrors tests/test_date_extraction.py's split between pure
pattern-matching tests and end-to-end extraction tests (the latter live
in tests/test_iep_service_extraction.py).
"""

from __future__ import annotations

from app.core.iep_extraction.patterns import find_service_line_matches


def test_finds_minutes_then_frequency_service_line():
    text = "Speech-language therapy — 30 minutes, 2x/week"
    matches = find_service_line_matches(text)
    assert len(matches) == 1
    match = matches[0]
    assert match.service_name == "Speech-language therapy"
    assert match.minutes == 30
    assert match.frequency_count == 2
    assert match.frequency_period == "week"
    assert match.location is None
    assert match.provider is None


def test_offsets_satisfy_the_line_slice_invariant():
    text = "Intro line.\nSpeech-language therapy — 30 minutes, 2x/week\nTrailing line.\n"
    matches = find_service_line_matches(text)
    assert len(matches) == 1
    match = matches[0]
    assert text[match.line_start : match.line_end] == match.line_text


def test_offsets_correct_with_leading_whitespace_and_marker():
    text = "  1. Occupational therapy - 20 minutes, 1x/week\n"
    matches = find_service_line_matches(text)
    assert len(matches) == 1
    match = matches[0]
    assert text[match.line_start : match.line_end] == match.line_text
    assert match.service_name == "Occupational therapy"


def test_does_not_match_frequency_then_minutes_order():
    """Deliberately narrow -- only minutes-then-frequency order is
    supported (see module docstring)."""
    matches = find_service_line_matches("Speech therapy 2x/week for 30 minutes")
    assert matches == []


def test_no_match_on_plain_text():
    matches = find_service_line_matches("This is just a regular sentence with no services in it.")
    assert matches == []


def test_rejects_out_of_range_minutes():
    matches = find_service_line_matches("Mystery service — 999 minutes, 2x/week")
    assert matches == []


def test_rejects_out_of_range_frequency_count():
    matches = find_service_line_matches("Mystery service — 30 minutes, 99x/week")
    assert matches == []


def test_rejects_line_with_no_usable_service_name():
    """Nothing but digits/punctuation before the match -- no letters to
    form a service name, so the line is silently skipped."""
    matches = find_service_line_matches("30 minutes, 2x/week")
    assert matches == []


def test_captures_location_trailer():
    matches = find_service_line_matches("Counseling — 45 minutes, 1x/week in the resource room")
    assert len(matches) == 1
    assert matches[0].location == "the resource room"
    assert matches[0].provider is None


def test_captures_provider_trailer():
    matches = find_service_line_matches("Counseling — 45 minutes, 1x/week by the school psychologist")
    assert len(matches) == 1
    assert matches[0].provider == "the school psychologist"
    assert matches[0].location is None


def test_finds_multiple_service_lines_on_separate_lines():
    text = (
        "Speech-language therapy — 30 minutes, 2x/week\n"
        "Occupational therapy — 20 minutes, 1x/week\n"
    )
    matches = find_service_line_matches(text)
    assert len(matches) == 2
    assert [m.service_name for m in matches] == ["Speech-language therapy", "Occupational therapy"]
    for match in matches:
        assert text[match.line_start : match.line_end] == match.line_text


def test_frequency_period_is_lowercased():
    matches = find_service_line_matches("Reading support — 30 minutes, 3x/Week")
    assert len(matches) == 1
    assert matches[0].frequency_period == "week"


def test_alternate_period_words_supported():
    matches = find_service_line_matches("Counseling — 30 minutes, 1x/month")
    assert len(matches) == 1
    assert matches[0].frequency_period == "month"


def test_empty_text_returns_no_matches():
    assert find_service_line_matches("") == []
