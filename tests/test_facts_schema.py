"""Schema tests for the Phase 3.5 Step 1 Fact, Observation & Summary Layer.

See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "Fact, Observation &
Summary Layer". No core module exists yet (that's Step 2) -- these tests
exercise the ORM models directly to confirm the schema itself (tables,
foreign keys, the promotion-lineage relationship) is correct before any
application logic is built on top of it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AiObservation,
    AiObservationCitation,
    AiSummary,
    Case,
    Citation,
    Document,
    FactType,
    SummarySourceDocument,
    VerifiedFact,
    VerifiedFactCitation,
)


def _document(db: Session, case: Case) -> Document:
    document = Document(
        case_id=case.case_id,
        original_filename="letter.pdf",
        stored_path="cases/1/documents/letter.pdf",
        sha256_hash="a" * 64,
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


def _fact_type(db: Session, name: str = "date") -> FactType:
    return db.scalar(select(FactType).where(FactType.name == name))


def test_ai_observation_round_trips_with_citation(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact_type = _fact_type(db_session)

    observation = AiObservation(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="Possible meeting date: 2024-03-12",
        confidence_score=0.8,
        method="regex-date-parse-v1",
    )
    db_session.add(observation)
    db_session.flush()
    db_session.add(AiObservationCitation(observation_id=observation.observation_id, citation_id=citation.citation_id))
    db_session.commit()

    stored = db_session.get(AiObservation, observation.observation_id)
    assert stored.status == "pending_review"
    assert stored.reviewed_by is None
    assert stored.reviewed_at is None

    links = db_session.scalars(
        select(AiObservationCitation).where(AiObservationCitation.observation_id == observation.observation_id)
    ).all()
    assert [link.citation_id for link in links] == [citation.citation_id]


def test_verified_fact_created_directly_has_no_source_observation(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact_type = _fact_type(db_session, "date")

    fact = VerifiedFact(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="IEP annual review meeting held",
        confidence_label="certain",
        created_by="test-user",
    )
    db_session.add(fact)
    db_session.flush()
    db_session.add(VerifiedFactCitation(fact_id=fact.fact_id, citation_id=citation.citation_id))
    db_session.commit()

    stored = db_session.get(VerifiedFact, fact.fact_id)
    assert stored.source_observation_id is None
    assert stored.confidence_score is None
    assert stored.deleted_at is None


def test_verified_fact_promoted_from_observation_carries_lineage(db_session: Session, sample_case: Case):
    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact_type = _fact_type(db_session, "date")

    observation = AiObservation(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="Possible meeting date: 2024-03-12",
        confidence_score=0.8,
        method="regex-date-parse-v1",
    )
    db_session.add(observation)
    db_session.flush()

    fact = VerifiedFact(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="IEP annual review meeting held March 12, 2024",
        confidence_label="probable",
        confidence_score=observation.confidence_score,
        source_observation_id=observation.observation_id,
        created_by="test-user",
    )
    db_session.add(fact)
    db_session.commit()

    stored = db_session.get(VerifiedFact, fact.fact_id)
    assert stored.source_observation.observation_id == observation.observation_id
    assert stored.confidence_score == 0.8

    # Promoting never mutates the originating observation row itself --
    # only a future promote_observation() (Step 2) sets status/reviewed_*,
    # and this test constructs the fact directly without calling it.
    reloaded_observation = db_session.get(AiObservation, observation.observation_id)
    assert reloaded_observation.status == "pending_review"


def test_verified_fact_soft_delete_leaves_row_in_place(db_session: Session, sample_case: Case):
    import datetime

    document = _document(db_session, sample_case)
    citation = _citation(db_session, document)
    fact_type = _fact_type(db_session, "date")

    fact = VerifiedFact(
        case_id=sample_case.case_id,
        fact_type_id=fact_type.type_id,
        statement="IEP annual review meeting held",
        confidence_label="certain",
        created_by="test-user",
    )
    db_session.add(fact)
    db_session.commit()

    fact.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db_session.commit()

    stored = db_session.get(VerifiedFact, fact.fact_id)
    assert stored is not None
    assert stored.statement == "IEP annual review meeting held"
    assert stored.deleted_at is not None


def test_ai_summary_and_source_documents_schema_only(db_session: Session, sample_case: Case):
    """No generator exists yet (Phase 3.5 Step 1 is schema-only) -- this
    confirms the table shape itself is correct by inserting a row by
    hand, the way a future Phase 8 generator eventually would.
    """
    document = _document(db_session, sample_case)

    summary = AiSummary(
        case_id=sample_case.case_id,
        scope="document",
        scope_document_id=document.document_id,
        summary_text="Placeholder summary text.",
        generated_by="manual-test",
        label_text="AI-Generated Summary — Unverified, Requires Human Review. Not a Fact or Legal Conclusion.",
    )
    db_session.add(summary)
    db_session.flush()
    db_session.add(SummarySourceDocument(summary_id=summary.summary_id, document_id=document.document_id))
    db_session.commit()

    stored = db_session.get(AiSummary, summary.summary_id)
    assert stored.review_status == "pending_review"
    assert stored.reviewed_by is None

    sources = db_session.scalars(
        select(SummarySourceDocument).where(SummarySourceDocument.summary_id == summary.summary_id)
    ).all()
    assert [s.document_id for s in sources] == [document.document_id]
