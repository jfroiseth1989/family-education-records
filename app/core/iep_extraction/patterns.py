"""Deterministic service-line pattern matching (no AI, no network).

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md §3.2 #1. Scans one page's worth
of already-extracted text, line by line, for the shape "<service name>
<N> minutes ... <N>x/<period>[ <trailing location/provider text>]" --
the minutes-then-frequency order the plan describes, matching its own
mockup example ("Speech-language therapy — 30 minutes, 2x/week"). A
line that doesn't match this exact shape produces nothing -- never a
guess, the same discipline app/core/document_type_suggestion.py and
app/core/date_patterns.py already follow.

Matching is line-scoped, not free-roaming across a page, so the
"service name" component (everything on the line before the matched
minutes/frequency span) stays bounded and never accidentally spans
unrelated text -- the same bounded-window discipline
`_PRINTED_EMAIL_HEADER_BLOCK` in document_type_suggestion.py uses for
its own multi-part trigger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MIN_MINUTES = 1
_MAX_MINUTES = 600
_MIN_FREQUENCY_COUNT = 1
_MAX_FREQUENCY_COUNT = 31

_MIN_SERVICE_NAME_LENGTH = 2
_MAX_SERVICE_NAME_LENGTH = 150
_MAX_TRAILER_LENGTH = 60

# Minutes, then (within a short non-digit gap) a frequency count/period.
# Deliberately only this one order -- see module docstring.
_SERVICE_LINE_PATTERN = re.compile(
    r"(?P<minutes>\d{1,3})\s*(?:minutes?|mins?)\b"
    r"[^\d]{0,15}?"
    r"(?P<count>\d{1,2})\s*(?:x\b|times?\b)\s*(?:/|per)\s*(?P<period>week|month|day|session|quarter|year)s?\b",
    re.IGNORECASE,
)

# Strips a leading list marker ("1.", "-", "•", "(a)") before validating
# the remaining text as a service name -- cosmetic only, never changes
# whether a line matches.
_LEADING_MARKER_PATTERN = re.compile(r"^[\s\-–—•*]*(?:\(?\d{1,2}[.)])?[\s\-–—•*]*")

_LOCATION_TRAILER_PATTERN = re.compile(r"^(?:in|at)\s+(?P<location>.+)$", re.IGNORECASE)
_PROVIDER_TRAILER_PATTERN = re.compile(r"^(?:by|provided by)\s+(?P<provider>.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ServiceLineMatch:
    """One matched service line, with offsets relative to the page text
    it came from -- `text[line_start:line_end] == line_text` always
    holds, the same invariant `create_highlight()` relies on for its
    own citation offsets.
    """

    line_start: int
    line_end: int
    line_text: str
    service_name: str
    minutes: int
    frequency_count: int
    frequency_period: str
    location: str | None
    provider: str | None


def _clean_service_name(raw: str) -> str | None:
    without_marker = _LEADING_MARKER_PATTERN.sub("", raw)
    cleaned = without_marker.strip(" \t-–—:•")
    if not (_MIN_SERVICE_NAME_LENGTH <= len(cleaned) <= _MAX_SERVICE_NAME_LENGTH):
        return None
    if not re.search(r"[A-Za-z]", cleaned):
        return None
    return cleaned


def _trailing_descriptor(raw: str) -> tuple[str | None, str | None]:
    """Best-effort (location, provider) from the text after the
    frequency match -- either, neither, but never both at once.
    """
    trailing = raw.strip(" \t-–—,;:")
    if not trailing or len(trailing) > _MAX_TRAILER_LENGTH:
        return None, None
    location_match = _LOCATION_TRAILER_PATTERN.match(trailing)
    if location_match:
        return location_match.group("location").strip(), None
    provider_match = _PROVIDER_TRAILER_PATTERN.match(trailing)
    if provider_match:
        return None, provider_match.group("provider").strip()
    return None, None


def find_service_line_matches(text: str) -> list[ServiceLineMatch]:
    """Scan `text` (one page's effective text) line by line for service
    lines.

    Returns one `ServiceLineMatch` per matching line; a line that
    doesn't cleanly match (no minutes+frequency pattern, an
    out-of-range minutes/frequency value, or no usable service-name
    text before the match) produces nothing for that line -- silence,
    never a guess.
    """
    matches: list[ServiceLineMatch] = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        line_len = len(raw_line)
        stripped_line = raw_line.rstrip("\r\n")
        leading_ws = len(stripped_line) - len(stripped_line.lstrip())
        line_start = cursor + leading_ws
        trimmed = stripped_line.strip()
        line_end = line_start + len(trimmed)
        cursor += line_len

        if not trimmed:
            continue

        match = _SERVICE_LINE_PATTERN.search(trimmed)
        if not match:
            continue

        minutes = int(match.group("minutes"))
        count = int(match.group("count"))
        if not (_MIN_MINUTES <= minutes <= _MAX_MINUTES):
            continue
        if not (_MIN_FREQUENCY_COUNT <= count <= _MAX_FREQUENCY_COUNT):
            continue

        service_name = _clean_service_name(trimmed[: match.start()])
        if service_name is None:
            continue

        location, provider = _trailing_descriptor(trimmed[match.end() :])

        matches.append(
            ServiceLineMatch(
                line_start=line_start,
                line_end=line_end,
                line_text=trimmed,
                service_name=service_name,
                minutes=minutes,
                frequency_count=count,
                frequency_period=match.group("period").lower(),
                location=location,
                provider=provider,
            )
        )
    return matches
