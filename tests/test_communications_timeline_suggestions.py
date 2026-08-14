"""Tests for app/core/communications/timeline_suggestions.py
(Communications Phase Step 7): deterministic candidate generation,
idempotency/dedup, and the reused Facts promotion/rejection pipeline
end-to-end for a Communication-sourced observation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.ingestion import import_eml_file
from app.core.communications.timeline_suggestions import (
    compose_candidate_statement,
    generate_timeline_suggestion,
)
from app.core.facts.service import promote_observation, reject_observation
from app.core.files import compute_sha256
from app.core.timeline.service import create_timeline_event, list_date_source_candidates
from app.core.vault import VaultLayout
from app.db.models import AiObservation, Case, Communication, TimelineEvent, TimelineEventFact, VerifiedFact


def _eml(
    *,
    message_id: str,
    subject: str | None = "IEP Meeting Notice",
    from_addr: str | None = "amanda.wagner@district.example.org",
    from_name: str | None = "Amanda Wagner",
    date: str | None = "Mon, 7 Mar 2022 14:30:00 -0500",
) -> bytes:
    lines = []
    if from_addr:
        lines.append(f"From: {from_name} <{from_addr}>" if from_name else f"From: {from_addr}")
    lines.append("To: parent@yahoo.com")
    if subject is not None:
        lines.append(f"Subject: {subject}")
    if date is not None:
        lines.append(f"Date: {date}")
    lines.append(f"Message-ID: {message_id}")
    lines.append("")
    lines.append("Body text is never read by the suggestion generator.")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _write(tmp_path: Path, raw: bytes, name: str) -> Path:
    path = tmp_path / "source-files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _import(db: Session, vault: VaultLayout, case: Case, tmp_path: Path, raw: bytes, name: str) -> Communication:
    source = _write(tmp_path, raw, name)
    communication = import_eml_file(
        db, vault, case, source_file_path=source, original_filename=name, actor="test-user"
    )
    db.commit()
    return communication


# --- compose_candidate_statement: pure function -----------------------


def test_candidate_statement_format():
    communication = Communication(
        communication_type="email",
        subject="IEP Meeting Notice",
        from_display_name="Amanda Wagner",
        from_address="amanda.wagner@district.example.org",
        sent_at=datetime(2022, 3, 7, 14, 30, tzinfo=timezone.utc),
        sha256_hash="a" * 64, stored_path="p", file_size_bytes=1,
        import_method="manual_upload", imported_by="t",
    )
    result = compose_candidate_statement(communication)
    assert result is not None
    statement, moment, basis = result
    assert statement == 'Email received from Amanda Wagner regarding "IEP Meeting Notice" on 2022-03-07.'
    assert moment == datetime(2022, 3, 7, 14, 30, tzinfo=timezone.utc)
    assert basis == "sent"


def test_candidate_prefers_received_at_over_sent_at():
    communication = Communication(
        communication_type="email", subject="Subj",
        from_address="x@example.org",
        sent_at=datetime(2022, 3, 7, tzinfo=timezone.utc),
        received_at=datetime(2022, 3, 8, tzinfo=timezone.utc),
        sha256_hash="a" * 64, stored_path="p", file_size_bytes=1,
        import_method="manual_upload", imported_by="t",
    )
    _, moment, basis = compose_candidate_statement(communication)
    assert moment == datetime(2022, 3, 8, tzinfo=timezone.utc)
    assert basis == "received"


def test_candidate_falls_back_to_unknown_sender_and_subject():
    communication = Communication(
        communication_type="email", subject=None,
        from_display_name=None, from_address=None,
        sent_at=datetime(2022, 3, 7, tzinfo=timezone.utc),
        sha256_hash="a" * 64, stored_path="p", file_size_bytes=1,
        import_method="manual_upload", imported_by="t",
    )
    statement, _, _ = compose_candidate_statement(communication)
    assert statement == 'Email received from an unknown sender regarding "(no subject)" on 2022-03-07.'


def test_candidate_none_when_no_date_at_all():
    communication = Communication(
        communication_type="email", subject="Subj", from_address="x@example.org",
        sent_at=None, received_at=None,
        sha256_hash="a" * 64, stored_path="p", file_size_bytes=1,
        import_method="manual_upload", imported_by="t",
    )
    assert compose_candidate_statement(communication) is None


# --- generate_timeline_suggestion: service-level -----------------------


def test_generate_creates_pending_observation(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<a@x>"), "a.eml")

    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()
    assert observation.status == "pending_review"
    assert observation.case_id == sample_case.case_id
    assert observation.fact_type.name == "date"
    assert observation.communication_id == communication.communication_id
    assert observation.statement == (
        'Email received from Amanda Wagner regarding "IEP Meeting Notice" on 2022-03-07.'
    )


def test_generate_returns_none_when_no_date(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(
        db_session, vault, sample_case, tmp_path, _eml(message_id="<b@x>", date=None), "b.eml"
    )
    assert db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).count() == 0


def test_generate_is_idempotent_no_duplicate_pending(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<c@x>"), "c.eml")

    # Re-run generation explicitly (import already ran it once).
    result = generate_timeline_suggestion(db_session, communication)
    db_session.commit()

    assert result is None
    assert db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).count() == 1


def test_rejected_suggestion_not_regenerated(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<d@x>"), "d.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()

    reject_observation(db_session, observation, actor="test-user")
    db_session.commit()

    result = generate_timeline_suggestion(db_session, communication)
    db_session.commit()

    assert result is None
    observations = db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).all()
    assert len(observations) == 1
    assert observations[0].status == "rejected"


def test_accepted_suggestion_not_regenerated(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<e@x>"), "e.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()

    promote_observation(db_session, observation, confidence_label="certain", actor="test-user")
    db_session.commit()

    result = generate_timeline_suggestion(db_session, communication)
    db_session.commit()

    assert result is None
    observations = db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).all()
    assert len(observations) == 1
    assert observations[0].status == "accepted"


def test_soft_deleted_communication_generates_no_suggestion(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<f@x>"), "f.eml")
    # Remove the automatically-generated one to test generation fresh.
    db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).delete()
    db_session.commit()

    communication.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    result = generate_timeline_suggestion(db_session, communication)
    assert result is None
    assert db_session.query(AiObservation).filter_by(communication_id=communication.communication_id).count() == 0


def test_communication_without_case_generates_no_suggestion():
    communication = Communication(
        communication_type="email", subject="Subj", from_address="x@example.org",
        sent_at=datetime(2022, 3, 7, tzinfo=timezone.utc), case_id=None,
        sha256_hash="a" * 64, stored_path="p", file_size_bytes=1,
        import_method="manual_upload", imported_by="t",
    )
    # No db needed -- case_id check short-circuits before any query.
    from unittest.mock import MagicMock

    result = generate_timeline_suggestion(MagicMock(), communication)
    assert result is None


# --- approve / edit+approve / reject -> reused Facts pipeline -----------


def test_approve_creates_verified_fact_linked_to_communication(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<g@x>"), "g.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()

    fact = promote_observation(db_session, observation, confidence_label="certain", actor="test-user")
    db_session.commit()

    assert fact.communication_id == communication.communication_id
    assert fact.source_observation_id == observation.observation_id
    assert fact.statement == observation.statement
    assert fact.fact_date == observation.observed_date

    db_session.refresh(observation)
    assert observation.status == "accepted"
    assert observation.reviewed_by == "test-user"


def test_edit_and_approve_preserves_edited_text_and_original(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<h@x>"), "h.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()
    original_statement = observation.statement

    fact = promote_observation(
        db_session, observation, confidence_label="certain", actor="test-user",
        statement="Edited: family confirmed receipt of the IEP meeting notice.",
    )
    db_session.commit()

    assert fact.statement == "Edited: family confirmed receipt of the IEP meeting notice."
    db_session.refresh(observation)
    assert observation.statement == original_statement  # untouched


def test_reject_creates_no_fact_and_is_permanent(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<i@x>"), "i.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()

    reject_observation(db_session, observation, actor="test-user", reason="not relevant")
    db_session.commit()

    assert db_session.query(VerifiedFact).filter_by(communication_id=communication.communication_id).count() == 0
    db_session.refresh(observation)
    assert observation.status == "rejected"

    with pytest.raises(ValueError):
        promote_observation(db_session, observation, confidence_label="certain", actor="test-user")


def test_approved_communication_fact_can_anchor_a_timeline_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """Reuses app/core/timeline/service.py completely unmodified -- proves
    a Communication-sourced fact is eligible exactly like a Document-
    sourced one, per docs/COMMUNICATIONS_PLAN.md Step 7's "reuse the
    existing verified fact/timeline-event promotion path" requirement.
    """
    communication = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<j@x>"), "j.eml")
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()
    fact = promote_observation(db_session, observation, confidence_label="certain", actor="test-user")
    db_session.commit()

    candidates = list_date_source_candidates(db_session, sample_case.case_id)
    assert fact.fact_id in [c.fact_id for c in candidates]

    event = create_timeline_event(
        db_session, sample_case, "communication", "IEP meeting notice received",
        date_fact_id=fact.fact_id, actor="test-user",
    )
    db_session.commit()

    assert event.event_date == fact.fact_date
    link = db_session.get(TimelineEventFact, (event.event_id, fact.fact_id))
    assert link is not None
    assert link.is_date_source is True


# --- integrity: raw email/attachment/custody untouched ----------------


def test_suggestion_generation_never_modifies_raw_email(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write(tmp_path, _eml(message_id="<k@x>"), "k.eml")
    original_hash = compute_sha256(source)

    communication = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="k.eml", actor="test-user"
    )
    db_session.commit()

    stored_path = vault.root / communication.stored_path
    assert compute_sha256(stored_path) == original_hash
    assert communication.subject == "IEP Meeting Notice"
    assert communication.body_text is not None

    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).one()
    promote_observation(db_session, observation, confidence_label="certain", actor="test-user")
    db_session.commit()

    db_session.refresh(communication)
    assert compute_sha256(stored_path) == original_hash
    assert communication.subject == "IEP Meeting Notice"
