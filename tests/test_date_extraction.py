"""Tests for app/core/facts/date_extraction.py -- Phase 3.5 Step 3.

See docs/PROJECT_PLAN.md decision #11. Deterministic regex date-parser,
no model -- tests both the pure pattern-matching (`_find_date_matches`)
and the end-to-end `extract_date_observations()` against real DB rows.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.facts.date_extraction import METHOD, _find_date_matches, extract_date_observations
from app.db.models import AiObservation, AiObservationCitation, Case, Citation, Document, DocumentPage


_document_counter = 0


def _document(db: Session, case: Case) -> Document:
    global _document_counter
    _document_counter += 1
    filename = f"letter-{_document_counter}.pdf"
    digest = hashlib.sha256(f"{case.case_id}:{filename}:{_document_counter}".encode()).hexdigest()
    document = Document(
        case_id=case.case_id,
        original_filename=filename,
        stored_path=f"cases/{case.case_id}/documents/{filename}",
        sha256_hash=digest,
        file_size_bytes=100,
        ingested_by="test-user",
    )
    db.add(document)
    db.flush()
    return document


def _page(db: Session, document: Document, text: str, page_number: int = 1) -> DocumentPage:
    page = DocumentPage(
        document_id=document.document_id,
        page_number=page_number,
        extracted_text=text,
        extraction_method="native",
        char_count=len(text),
        source_sha256=document.sha256_hash,
    )
    db.add(page)
    db.flush()
    return page


# --- pure pattern matching --------------------------------------------------


def test_finds_full_month_name_date():
    matches = _find_date_matches("The meeting was held on March 12, 2024 at the school.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2024, 3, 12)
    assert matches[0].confidence_score == 0.9


def test_finds_abbreviated_month_name_date():
    matches = _find_date_matches("Evaluation dated Sept. 3, 2023.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2023, 9, 3)


def test_finds_month_name_date_with_ordinal_suffix():
    matches = _find_date_matches("Signed on January 1st, 2022.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2022, 1, 1)


def test_finds_iso_date():
    matches = _find_date_matches("Record ID 2024-03-12 shows the review.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2024, 3, 12)
    assert matches[0].confidence_score == 0.85


def test_finds_numeric_us_date():
    matches = _find_date_matches("Due by 03/12/2024 per the notice.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2024, 3, 12)
    assert matches[0].confidence_score == 0.6


def test_finds_numeric_dash_date():
    matches = _find_date_matches("Due by 03-12-2024 per the notice.")
    assert len(matches) == 1
    assert matches[0].parsed_date == date(2024, 3, 12)


def test_rejects_invalid_calendar_date():
    matches = _find_date_matches("Reference 13/45/2024 is not a real date.")
    assert matches == []


def test_rejects_invalid_february_date():
    matches = _find_date_matches("See Feb. 30, 2024 for details.")
    assert matches == []


def test_does_not_match_two_digit_year():
    """2-digit years are deliberately never matched -- see the module
    docstring on why guessing the century would not be deterministic.
    """
    matches = _find_date_matches("Filed 03/12/24 at the office.")
    assert matches == []


def test_rejects_year_outside_plausible_range():
    matches = _find_date_matches("Case number 03/12/1850 filed.")
    assert matches == []


def test_no_matches_in_plain_text():
    matches = _find_date_matches("This document has no dates in it at all.")
    assert matches == []


def test_finds_multiple_distinct_dates():
    matches = _find_date_matches("Held March 12, 2024 and reviewed 2024-06-01.")
    assert len(matches) == 2
    assert {m.parsed_date for m in matches} == {date(2024, 3, 12), date(2024, 6, 1)}


def test_match_offsets_are_correct():
    text = "Prefix text. March 12, 2024 suffix text."
    matches = _find_date_matches(text)
    assert len(matches) == 1
    dm = matches[0]
    assert text[dm.start:dm.end] == dm.matched_text
    assert dm.matched_text == "March 12, 2024"


# --- end-to-end extraction --------------------------------------------------


def test_extract_creates_observation_with_citation(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "The IEP meeting was held on March 12, 2024.")

    created = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert len(created) == 1
    observation = db_session.get(AiObservation, created[0].observation_id)
    assert observation.statement == "Possible date: 2024-03-12"
    assert observation.method == METHOD
    assert observation.status == "pending_review"
    assert observation.observed_date == datetime(2024, 3, 12, tzinfo=timezone.utc)

    link = db_session.scalars(
        select(AiObservationCitation).where(AiObservationCitation.observation_id == observation.observation_id)
    ).one()
    citation = db_session.get(Citation, link.citation_id)
    assert citation.quoted_text == "March 12, 2024"
    assert citation.page_id == page.page_id
    assert citation.text_source == "native"


def test_extract_finds_nothing_on_a_page_with_no_dates(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "This page has no dates on it whatsoever.")

    created = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert created == []


def test_extract_scans_every_page(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "Held March 12, 2024.", page_number=1)
    _page(db_session, document, "Reviewed 2024-06-01.", page_number=2)

    created = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert len(created) == 2


def test_extract_is_idempotent_on_unchanged_text(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    _page(db_session, document, "Held March 12, 2024.")

    first = extract_date_observations(db_session, sample_case, document)
    db_session.commit()
    second = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert len(first) == 1
    assert second == []

    all_observations = db_session.scalars(select(AiObservation)).all()
    assert len(all_observations) == 1


def test_extract_finds_new_matches_after_page_text_changes(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = _page(db_session, document, "Held March 12, 2024.")

    first = extract_date_observations(db_session, sample_case, document)
    db_session.commit()
    assert len(first) == 1

    page.extracted_text = "Held March 12, 2024. Rescheduled to April 1, 2024."
    db_session.commit()

    second = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert len(second) == 1
    all_observations = db_session.scalars(select(AiObservation)).all()
    assert len(all_observations) == 2


def test_extract_ignores_pages_with_no_effective_text(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    page = DocumentPage(
        document_id=document.document_id,
        page_number=1,
        extraction_method="none",
        char_count=0,
        needs_ocr=True,
        source_sha256=document.sha256_hash,
    )
    db_session.add(page)
    db_session.commit()

    created = extract_date_observations(db_session, sample_case, document)
    db_session.commit()

    assert created == []
