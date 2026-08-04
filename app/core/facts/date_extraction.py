"""Deterministic date-observation extractor (Phase 3.5 Step 3).

See docs/PROJECT_PLAN.md decision #11 and docs/PHASE_3_DECISIONS.md.
Scans a document's pages for date-like substrings using fixed regular
expressions and calendar validation only -- no model, no fuzzy/AI
inference, no cloud call. Every match becomes an `ai_observations` row
(via app/core/facts/service.py::create_ai_observation()) with its own new
`citations` row pointing at the exact page/offset span matched, so a
human reviewing the observation can always see precisely what text
produced it. `effective_text()` (app/core/ocr/text.py) is used as the
scan source, so a page that needed OCR is scanned exactly like a
natively-extracted one, once Phase 3 has populated it.

The actual pattern matching (three fixed pattern families, calendar
validation, confidence scores) lives in app/core/date_patterns.py,
shared with app/core/document_date_suggestion.py (FERChronos document
drop-zone auto-fill) so both scan for "what does a date look like" the
same way. `_DateMatch`/`_find_date_matches` are kept here as aliases for
backward compatibility with existing callers/tests of this module.

Each match's parsed `date` is passed through as `observed_date` (Phase 4
Step 0) so a later promotion of the observation carries a real,
structured date into `verified_facts.fact_date` -- not just the
formatted string in `statement`.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.date_patterns import DateMatch as _DateMatch
from app.core.date_patterns import find_date_matches as _find_date_matches
from app.core.facts.service import create_ai_observation
from app.core.ocr.text import effective_text
from app.db.models import AiObservation, AiObservationCitation, Case, Citation, Document

SYSTEM_ACTOR = "system (regex-date-parse-v1)"
METHOD = "regex-date-parse-v1"
FACT_TYPE = "date"


def _observation_exists_for_span(db: Session, page_id: int, start_offset: int, end_offset: int) -> bool:
    """True if some observation already cites this exact page/span.

    Makes re-running the scan on a document safe: unchanged text never
    produces duplicate pending-review rows for a human to look at twice.
    A page whose text changed (e.g. after an OCR correction) gets new
    offsets for its matches, so this only ever suppresses true repeats.
    """
    existing = db.scalars(
        select(AiObservationCitation.observation_id)
        .join(Citation, Citation.citation_id == AiObservationCitation.citation_id)
        .where(
            Citation.page_id == page_id,
            Citation.start_offset == start_offset,
            Citation.end_offset == end_offset,
        )
        .limit(1)
    ).first()
    return existing is not None


def extract_date_observations(db: Session, case: Case, document: Document, actor: str = SYSTEM_ACTOR) -> list[AiObservation]:
    """Scan every page of `document` for date-like text and record observations.

    Returns the newly created observations (empty if nothing new was
    found -- either no date-like text exists, or every match was already
    observed by a prior scan). Does not commit -- same convention as
    every other core module; the caller controls the transaction boundary.
    """
    created: list[AiObservation] = []

    for page in sorted(document.pages, key=lambda p: p.page_number):
        resolved = effective_text(page)
        text = resolved.text or ""
        if not text:
            continue

        for dm in _find_date_matches(text):
            if _observation_exists_for_span(db, page.page_id, dm.start, dm.end):
                continue

            citation = Citation(
                document_id=document.document_id,
                page_id=page.page_id,
                start_offset=dm.start,
                end_offset=dm.end,
                quoted_text=dm.matched_text,
                text_source=resolved.source,
                source_confidence=resolved.confidence,
            )
            db.add(citation)
            db.flush()  # assigns citation.citation_id

            observation = create_ai_observation(
                db,
                case,
                FACT_TYPE,
                statement=f"Possible date: {dm.parsed_date.isoformat()}",
                confidence_score=dm.confidence_score,
                method=METHOD,
                citation_ids=[citation.citation_id],
                actor=actor,
                observed_date=datetime.combine(dm.parsed_date, time.min, tzinfo=timezone.utc),
            )
            created.append(observation)

    return created
