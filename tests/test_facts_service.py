"""Tests for app/core/facts/service.py -- Phase 3.5 Step 2.

See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "Fact, Observation &
Summary Layer".
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.facts.service import (
    ACCEPTED,
    PENDING_REVIEW,
    REJECTED,
    create_ai_observation,
    create_verified_fact,
    list_pending_observations,
    list_verified_facts,
    promote_observation,
    reject_observation,
)
from app.db.models import (
    AiObservation,
    AiObservationCitation,
    AuditLog,
    Case,
    Citation,
    Document,
    VerifiedFact,
    VerifiedFactCitation,
)


_document_counter = 0


def _document(db: Session, case: Case, filename: str | None = None) -> Document:
    global _document_counter
    _document_counter += 1
    filename = filename or f"letter-{_document_counter}.pdf"
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


def _citation(db: Session, document: Document, quoted_text: str = "IEP meeting held March 12, 2024") -> Citation:
    citation = Citation(document_id=document.document_id, quoted_text=quoted_text)
    db.add(citation)
    db.flush()
    return citation


# --- create_verified_fact ---------------------------------------------------


def test_create_verified_fact_succeeds(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    fact = create_verified_fact(
        db_session, sample_case, "category", "IEP annual review meeting held",
        "certain", [citation.citation_id], actor="test-user",
    )
    db_session.commit()

    stored = db_session.get(VerifiedFact, fact.fact_id)
    assert stored.statement == "IEP annual review meeting held"
    assert stored.confidence_label == "certain"
    assert stored.source_observation_id is None
    assert stored.confidence_score is None

    links = db_session.scalars(
        select(VerifiedFactCitation).where(VerifiedFactCitation.fact_id == fact.fact_id)
    ).all()
    assert [link.citation_id for link in links] == [citation.citation_id]

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "verified_fact_created")
    ).all()
    assert len(entries) == 1
    assert entries[0].entity_id == fact.fact_id
    assert entries[0].actor == "test-user"


def test_create_verified_fact_rejects_empty_statement(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="cannot be empty"):
        create_verified_fact(db_session, sample_case, "category", "   ", "certain", [citation.citation_id], actor="test-user")


def test_create_verified_fact_rejects_invalid_confidence_label(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="Invalid confidence label"):
        create_verified_fact(
            db_session, sample_case, "category", "A statement", "very sure",
            [citation.citation_id], actor="test-user",
        )


def test_create_verified_fact_rejects_empty_citation_list(db_session: Session, sample_case: Case):
    with pytest.raises(ValueError, match="At least one citation"):
        create_verified_fact(db_session, sample_case, "category", "A statement", "certain", [], actor="test-user")


def test_create_verified_fact_rejects_nonexistent_citation(db_session: Session, sample_case: Case):
    with pytest.raises(ValueError, match="not found"):
        create_verified_fact(db_session, sample_case, "category", "A statement", "certain", [99999], actor="test-user")


def test_create_verified_fact_rejects_citation_from_another_case(db_session: Session, sample_case: Case):
    other_case = Case(label="Other Case")
    db_session.add(other_case)
    db_session.flush()
    other_document = _document(db_session, other_case, filename="other.pdf")
    other_citation = _citation(db_session, other_document)

    with pytest.raises(ValueError, match="different case"):
        create_verified_fact(
            db_session, sample_case, "category", "A statement", "certain",
            [other_citation.citation_id], actor="test-user",
        )


def test_create_verified_fact_rejects_unknown_fact_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="Unknown fact type"):
        create_verified_fact(
            db_session, sample_case, "not-a-real-type", "A statement", "certain",
            [citation.citation_id], actor="test-user",
        )


def test_create_verified_fact_accepts_multiple_citations(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation_a = _citation(db_session, document, "First excerpt")
    citation_b = _citation(db_session, document, "Second excerpt")

    fact = create_verified_fact(
        db_session, sample_case, "category", "A statement", "certain",
        [citation_a.citation_id, citation_b.citation_id], actor="test-user",
    )
    db_session.commit()

    links = db_session.scalars(
        select(VerifiedFactCitation).where(VerifiedFactCitation.fact_id == fact.fact_id)
    ).all()
    assert {link.citation_id for link in links} == {citation_a.citation_id, citation_b.citation_id}


# --- create_ai_observation --------------------------------------------------


def test_create_ai_observation_succeeds(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    observation = create_ai_observation(
        db_session, sample_case, "category", "Possible meeting date: 2024-03-12",
        0.8, "regex-date-parse-v1", [citation.citation_id], actor="system (date-parser)",
    )
    db_session.commit()

    stored = db_session.get(AiObservation, observation.observation_id)
    assert stored.status == PENDING_REVIEW
    assert stored.confidence_score == 0.8
    assert stored.method == "regex-date-parse-v1"
    assert stored.reviewed_by is None

    links = db_session.scalars(
        select(AiObservationCitation).where(AiObservationCitation.observation_id == observation.observation_id)
    ).all()
    assert [link.citation_id for link in links] == [citation.citation_id]

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "ai_observation_created")
    ).all()
    assert len(entries) == 1
    assert entries[0].actor == "system (date-parser)"


def test_create_ai_observation_rejects_out_of_range_confidence(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        create_ai_observation(
            db_session, sample_case, "category", "A statement", 1.5, "method-v1",
            [citation.citation_id], actor="system",
        )


def test_create_ai_observation_rejects_empty_method(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="Method cannot be empty"):
        create_ai_observation(
            db_session, sample_case, "category", "A statement", 0.5, "   ",
            [citation.citation_id], actor="system",
        )


def test_create_ai_observation_rejects_empty_citation_list(db_session: Session, sample_case: Case):
    with pytest.raises(ValueError, match="At least one citation"):
        create_ai_observation(db_session, sample_case, "category", "A statement", 0.5, "method-v1", [], actor="system")


# --- promote_observation -----------------------------------------------


def _pending_observation(db: Session, case: Case, confidence_score: float = 0.8) -> AiObservation:
    document = _document(db, case)
    citation = _citation(db, document)
    return create_ai_observation(
        db, case, "category", "Possible meeting date: 2024-03-12", confidence_score,
        "regex-date-parse-v1", [citation.citation_id], actor="system (date-parser)",
    )


def test_promote_observation_creates_fact_with_lineage(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case, confidence_score=0.8)
    db_session.commit()

    fact = promote_observation(db_session, observation, "probable", actor="reviewer")
    db_session.commit()

    stored_fact = db_session.get(VerifiedFact, fact.fact_id)
    assert stored_fact.statement == observation.statement
    assert stored_fact.confidence_label == "probable"
    assert stored_fact.confidence_score == 0.8
    assert stored_fact.source_observation_id == observation.observation_id

    stored_observation = db_session.get(AiObservation, observation.observation_id)
    assert stored_observation.status == ACCEPTED
    assert stored_observation.reviewed_by == "reviewer"
    assert stored_observation.reviewed_at is not None
    # The observation's own claim text is never touched by promotion.
    assert stored_observation.statement == "Possible meeting date: 2024-03-12"


def test_promote_observation_copies_citations_to_the_new_fact(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()
    original_citation_ids = set(
        db_session.scalars(
            select(AiObservationCitation.citation_id).where(
                AiObservationCitation.observation_id == observation.observation_id
            )
        ).all()
    )

    fact = promote_observation(db_session, observation, "probable", actor="reviewer")
    db_session.commit()

    fact_citation_ids = set(
        db_session.scalars(
            select(VerifiedFactCitation.citation_id).where(VerifiedFactCitation.fact_id == fact.fact_id)
        ).all()
    )
    assert fact_citation_ids == original_citation_ids


def test_promote_observation_allows_statement_override(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()

    fact = promote_observation(
        db_session, observation, "certain", actor="reviewer",
        statement="IEP annual review meeting held March 12, 2024",
    )
    db_session.commit()

    assert fact.statement == "IEP annual review meeting held March 12, 2024"
    # Reviewer's correction never retroactively changes the observation's
    # own recorded claim.
    reloaded = db_session.get(AiObservation, observation.observation_id)
    assert reloaded.statement == "Possible meeting date: 2024-03-12"


def test_promote_observation_rejects_already_reviewed(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()
    promote_observation(db_session, observation, "certain", actor="reviewer")
    db_session.commit()

    with pytest.raises(ValueError, match="not pending review"):
        promote_observation(db_session, observation, "certain", actor="reviewer")


def test_promote_observation_rejects_invalid_confidence_label(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()

    with pytest.raises(ValueError, match="Invalid confidence label"):
        promote_observation(db_session, observation, "very sure", actor="reviewer")


def test_promote_observation_writes_two_audit_entries(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()

    fact = promote_observation(db_session, observation, "certain", actor="reviewer")
    db_session.commit()

    promoted_entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "ai_observation_promoted")
    ).all()
    fact_entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "verified_fact_created")
    ).all()
    assert len(promoted_entries) == 1
    assert promoted_entries[0].details["promoted_to_fact_id"] == fact.fact_id
    assert len(fact_entries) == 1
    assert fact_entries[0].details["source_observation_id"] == observation.observation_id


# --- reject_observation --------------------------------------------------


def test_reject_observation_marks_rejected_and_creates_no_fact(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()

    reject_observation(db_session, observation, actor="reviewer", reason="Not relevant to this case")
    db_session.commit()

    stored = db_session.get(AiObservation, observation.observation_id)
    assert stored.status == REJECTED
    assert stored.reviewed_by == "reviewer"
    assert stored.reviewed_at is not None

    facts = db_session.scalars(select(VerifiedFact)).all()
    assert facts == []

    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "ai_observation_rejected")
    ).all()
    assert len(entries) == 1
    assert entries[0].details["reason"] == "Not relevant to this case"


def test_reject_observation_rejects_already_reviewed(db_session: Session, sample_case: Case):
    observation = _pending_observation(db_session, sample_case)
    db_session.commit()
    reject_observation(db_session, observation, actor="reviewer")
    db_session.commit()

    with pytest.raises(ValueError, match="not pending review"):
        reject_observation(db_session, observation, actor="reviewer")


# --- listing helpers ---------------------------------------------------


def test_list_pending_observations_excludes_reviewed(db_session: Session, sample_case: Case):
    pending = _pending_observation(db_session, sample_case)
    db_session.commit()
    reviewed = _pending_observation(db_session, sample_case)
    db_session.commit()
    reject_observation(db_session, reviewed, actor="reviewer")
    db_session.commit()

    results = list_pending_observations(db_session, sample_case.case_id)
    assert [o.observation_id for o in results] == [pending.observation_id]


def test_list_verified_facts_excludes_soft_deleted(db_session: Session, sample_case: Case):
    import datetime

    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact = create_verified_fact(
        db_session, sample_case, "category", "A statement", "certain", [citation.citation_id], actor="test-user",
    )
    db_session.commit()

    results = list_verified_facts(db_session, sample_case.case_id)
    assert [f.fact_id for f in results] == [fact.fact_id]

    fact.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db_session.commit()

    results = list_verified_facts(db_session, sample_case.case_id)
    assert results == []


# --- fact_date / observed_date (Phase 4 Step 0) -----------------------


_MARCH_12 = datetime(2024, 3, 12, tzinfo=timezone.utc)
_APRIL_1 = datetime(2024, 4, 1, tzinfo=timezone.utc)


def test_create_verified_fact_stores_fact_date_for_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    fact = create_verified_fact(
        db_session, sample_case, "date", "IEP meeting held", "certain",
        [citation.citation_id], actor="test-user", fact_date=_MARCH_12,
    )
    db_session.commit()

    stored = db_session.get(VerifiedFact, fact.fact_id)
    assert stored.fact_date == _MARCH_12


def test_create_verified_fact_requires_fact_date_for_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="fact_date is required"):
        create_verified_fact(
            db_session, sample_case, "date", "IEP meeting held", "certain",
            [citation.citation_id], actor="test-user",
        )


def test_create_verified_fact_rejects_fact_date_for_non_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="only valid when fact_type is 'date'"):
        create_verified_fact(
            db_session, sample_case, "category", "Something categorical", "certain",
            [citation.citation_id], actor="test-user", fact_date=_MARCH_12,
        )


def test_create_ai_observation_stores_observed_date_for_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    observation = create_ai_observation(
        db_session, sample_case, "date", "Possible date: 2024-03-12", 0.8,
        "regex-date-parse-v1", [citation.citation_id], actor="system",
        observed_date=_MARCH_12,
    )
    db_session.commit()

    stored = db_session.get(AiObservation, observation.observation_id)
    assert stored.observed_date == _MARCH_12


def test_create_ai_observation_requires_observed_date_for_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="observed_date is required"):
        create_ai_observation(
            db_session, sample_case, "date", "Possible date: 2024-03-12", 0.8,
            "regex-date-parse-v1", [citation.citation_id], actor="system",
        )


def test_create_ai_observation_rejects_observed_date_for_non_date_type(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)

    with pytest.raises(ValueError, match="only valid when fact_type is 'date'"):
        create_ai_observation(
            db_session, sample_case, "category", "Something categorical", 0.8,
            "method-v1", [citation.citation_id], actor="system", observed_date=_MARCH_12,
        )


def _pending_date_observation(db: Session, case: Case, observed_date: datetime = _MARCH_12) -> AiObservation:
    document = _document(db, case)
    citation = _citation(db, document)
    return create_ai_observation(
        db, case, "date", f"Possible date: {observed_date.date().isoformat()}", 0.8,
        "regex-date-parse-v1", [citation.citation_id], actor="system (date-parser)",
        observed_date=observed_date,
    )


def test_promote_observation_carries_observed_date_into_fact_date(db_session: Session, sample_case: Case):
    observation = _pending_date_observation(db_session, sample_case)
    db_session.commit()

    fact = promote_observation(db_session, observation, "probable", actor="reviewer")
    db_session.commit()

    assert fact.fact_date == _MARCH_12


def test_promote_observation_allows_fact_date_override(db_session: Session, sample_case: Case):
    observation = _pending_date_observation(db_session, sample_case)
    db_session.commit()

    fact = promote_observation(
        db_session, observation, "certain", actor="reviewer", fact_date=_APRIL_1,
    )
    db_session.commit()

    assert fact.fact_date == _APRIL_1
    # The override never retroactively changes the observation's own
    # recorded date.
    reloaded = db_session.get(AiObservation, observation.observation_id)
    assert reloaded.observed_date == _MARCH_12


def test_promote_non_date_observation_has_no_fact_date(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    observation = create_ai_observation(
        db_session, sample_case, "category", "Something categorical", 0.7,
        "method-v1", [citation.citation_id], actor="system",
    )
    db_session.commit()

    fact = promote_observation(db_session, observation, "probable", actor="reviewer")
    db_session.commit()

    assert fact.fact_date is None
