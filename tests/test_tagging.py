"""Tests for app/core/tagging.py -- attaching/detaching case-scoped tags."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ingestion.service import ingest_document
from app.core.tagging import find_or_create_tag, list_case_tags, tag_document, untag_document
from app.core.vault import VaultLayout
from app.db.models import Case, DocumentTag, Tag


def _ingest(db, vault, case, path: Path, filename: str):
    document = ingest_document(db, vault, case, path, filename, actor="test-user")
    db.commit()
    return document


# --- find_or_create_tag -----------------------------------------------


def test_find_or_create_tag_creates_new_tag(db_session: Session, sample_case: Case):
    tag = find_or_create_tag(db_session, sample_case, "IEP", "record-type")
    db_session.commit()

    assert tag.tag_id is not None
    assert tag.name == "IEP"
    assert tag.category == "record-type"
    assert tag.case_id == sample_case.case_id


def test_find_or_create_tag_is_case_insensitive(db_session: Session, sample_case: Case):
    tag1 = find_or_create_tag(db_session, sample_case, "IEP")
    db_session.commit()
    tag2 = find_or_create_tag(db_session, sample_case, "iep")
    db_session.commit()
    tag3 = find_or_create_tag(db_session, sample_case, "  Iep  ")  # also strips whitespace
    db_session.commit()

    assert tag1.tag_id == tag2.tag_id == tag3.tag_id
    all_tags = db_session.query(Tag).filter_by(case_id=sample_case.case_id).all()
    assert len(all_tags) == 1


def test_find_or_create_tag_scoped_per_case(db_session: Session, sample_case: Case):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    tag_a = find_or_create_tag(db_session, sample_case, "IEP")
    db_session.commit()
    tag_b = find_or_create_tag(db_session, other_case, "IEP")
    db_session.commit()

    assert tag_a.tag_id != tag_b.tag_id
    assert tag_a.case_id == sample_case.case_id
    assert tag_b.case_id == other_case.case_id


def test_find_or_create_tag_rejects_empty_name(db_session: Session, sample_case: Case):
    with pytest.raises(ValueError):
        find_or_create_tag(db_session, sample_case, "   ")


def test_find_or_create_tag_without_category(db_session: Session, sample_case: Case):
    tag = find_or_create_tag(db_session, sample_case, "urgent")
    db_session.commit()
    assert tag.category is None


def test_database_rejects_duplicate_tag_name_in_same_case(
    db_session: Session, sample_case: Case
):
    """The application layer dedups case-insensitively; this confirms the
    database's case-sensitive UNIQUE constraint is also a real backstop.
    """
    db_session.add(Tag(case_id=sample_case.case_id, name="IEP"))
    db_session.commit()

    db_session.add(Tag(case_id=sample_case.case_id, name="IEP"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --- tag_document / untag_document -----------------------------------


def test_tag_document_attaches_tag_and_logs_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = _ingest(db_session, vault, sample_case, source_file, "doc.txt")

    tag = tag_document(db_session, document, "IEP", "record-type", actor="test-user")
    db_session.commit()

    assert tag.name == "IEP"
    link = db_session.get(DocumentTag, (document.document_id, tag.tag_id))
    assert link is not None

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "tagged"]
    tagged_event = document.custody_events[-1]
    assert tagged_event.details == {"tag_id": tag.tag_id, "tag_name": "IEP"}


def test_tag_document_is_idempotent(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = _ingest(db_session, vault, sample_case, source_file, "doc.txt")

    tag_document(db_session, document, "IEP", actor="test-user")
    db_session.commit()
    tag_document(db_session, document, "iep", actor="test-user")  # same tag, different case
    db_session.commit()

    links = db_session.query(DocumentTag).filter_by(document_id=document.document_id).all()
    assert len(links) == 1
    # No second "tagged" custody event for the no-op re-tag.
    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "tagged"]


def test_untag_document_removes_link_and_logs_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = _ingest(db_session, vault, sample_case, source_file, "doc.txt")
    tag = tag_document(db_session, document, "IEP", actor="test-user")
    db_session.commit()

    untag_document(db_session, document, tag.tag_id, actor="test-user")
    db_session.commit()

    link = db_session.get(DocumentTag, (document.document_id, tag.tag_id))
    assert link is None

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "tagged", "untagged"]


def test_untag_document_never_deletes_the_tag_itself(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = _ingest(db_session, vault, sample_case, source_file, "doc.txt")
    tag = tag_document(db_session, document, "IEP", actor="test-user")
    db_session.commit()

    untag_document(db_session, document, tag.tag_id, actor="test-user")
    db_session.commit()

    assert db_session.get(Tag, tag.tag_id) is not None


def test_untag_document_not_tagged_is_a_noop(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = _ingest(db_session, vault, sample_case, source_file, "doc.txt")
    tag = find_or_create_tag(db_session, sample_case, "unused-tag")
    db_session.commit()

    untag_document(db_session, document, tag.tag_id, actor="test-user")
    db_session.commit()

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported"]  # no "untagged" event logged for a no-op


# --- list_case_tags ------------------------------------------------


def test_list_case_tags_returns_sorted_case_scoped_tags(
    db_session: Session, sample_case: Case
):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    find_or_create_tag(db_session, sample_case, "Zebra")
    find_or_create_tag(db_session, sample_case, "Apple")
    find_or_create_tag(db_session, other_case, "ShouldNotAppear")
    db_session.commit()

    names = [t.name for t in list_case_tags(db_session, sample_case.case_id)]
    assert names == ["Apple", "Zebra"]
