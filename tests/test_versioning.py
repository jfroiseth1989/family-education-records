"""Tests for app/core/ingestion/versioning.py -- linking documents as versions."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ingestion.service import ingest_document
from app.core.ingestion.versioning import VersionLinkError, link_as_new_version
from app.core.vault import VaultLayout
from app.db.models import Case, Document


def _ingest(db, vault, case, path: Path, name: str) -> Document:
    doc = ingest_document(db, vault, case, path, name, actor="test-user")
    db.commit()
    return doc


def test_link_as_new_version_creates_group_and_flips_current(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    original = tmp_path / "iep-v1.txt"
    original.write_text("version one")
    corrected = tmp_path / "iep-v2.txt"
    corrected.write_text("version two, corrected date")

    doc_v1 = _ingest(db_session, vault, sample_case, original, "iep-v1.txt")
    doc_v2 = _ingest(db_session, vault, sample_case, corrected, "iep-v2.txt")

    group = link_as_new_version(
        db_session, doc_v1, doc_v2, actor="test-user", version_note="corrected date"
    )
    db_session.commit()

    assert doc_v1.version_group_id == group.group_id
    assert doc_v2.version_group_id == group.group_id
    assert doc_v1.version_number == 1
    assert doc_v2.version_number == 2
    assert doc_v1.is_current_version is False
    assert doc_v2.is_current_version is True
    assert doc_v2.supersedes_document_id == doc_v1.document_id
    assert doc_v2.version_note == "corrected date"
    assert group.current_document_id == doc_v2.document_id


def test_link_as_new_version_never_modifies_prior_version_content(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    original = tmp_path / "iep-v1.txt"
    original.write_text("version one")
    corrected = tmp_path / "iep-v2.txt"
    corrected.write_text("version two")

    doc_v1 = _ingest(db_session, vault, sample_case, original, "iep-v1.txt")
    original_hash = doc_v1.sha256_hash
    original_stored_path = doc_v1.stored_path

    doc_v2 = _ingest(db_session, vault, sample_case, corrected, "iep-v2.txt")
    link_as_new_version(db_session, doc_v1, doc_v2, actor="test-user")
    db_session.commit()

    # Only the version-relationship columns and is_current_version change;
    # everything establishing "what this document is" stays fixed.
    assert doc_v1.sha256_hash == original_hash
    assert doc_v1.stored_path == original_stored_path
    stored_file = vault.root / doc_v1.stored_path
    assert stored_file.read_text() == "version one"


def test_link_as_new_version_writes_custody_events_on_both_documents(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    original = tmp_path / "iep-v1.txt"
    original.write_text("version one")
    corrected = tmp_path / "iep-v2.txt"
    corrected.write_text("version two")

    doc_v1 = _ingest(db_session, vault, sample_case, original, "iep-v1.txt")
    doc_v2 = _ingest(db_session, vault, sample_case, corrected, "iep-v2.txt")
    link_as_new_version(db_session, doc_v1, doc_v2, actor="test-user")
    db_session.commit()

    v1_event_types = [e.event_type for e in doc_v1.custody_events]
    v2_event_types = [e.event_type for e in doc_v2.custody_events]

    assert v1_event_types == ["imported", "version_superseded"]
    assert v2_event_types == ["imported", "version_linked"]


def test_third_version_increments_version_number_and_reuses_group(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    f1, f2, f3 = (tmp_path / f"v{i}.txt" for i in (1, 2, 3))
    for f, text in zip((f1, f2, f3), ("one", "two", "three")):
        f.write_text(text)

    doc1 = _ingest(db_session, vault, sample_case, f1, "v1.txt")
    doc2 = _ingest(db_session, vault, sample_case, f2, "v2.txt")
    link_as_new_version(db_session, doc1, doc2, actor="test-user")
    db_session.commit()

    doc3 = _ingest(db_session, vault, sample_case, f3, "v3.txt")
    group = link_as_new_version(db_session, doc2, doc3, actor="test-user")
    db_session.commit()

    assert doc3.version_number == 3
    assert doc3.supersedes_document_id == doc2.document_id
    assert doc2.is_current_version is False
    assert doc3.is_current_version is True
    assert group.current_document_id == doc3.document_id

    all_group_docs = db_session.query(Document).filter(
        Document.version_group_id == group.group_id
    ).all()
    assert {d.document_id for d in all_group_docs} == {doc1.document_id, doc2.document_id, doc3.document_id}


def test_link_as_new_version_rejects_cross_case_linking(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    other_case = Case(label="Other case")
    db_session.add(other_case)
    db_session.commit()

    f1, f2 = tmp_path / "a.txt", tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")

    doc1 = _ingest(db_session, vault, sample_case, f1, "a.txt")
    doc2 = _ingest(db_session, vault, other_case, f2, "b.txt")

    with pytest.raises(VersionLinkError):
        link_as_new_version(db_session, doc1, doc2, actor="test-user")


def test_link_as_new_version_rejects_self_link(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    f1 = tmp_path / "a.txt"
    f1.write_text("a")
    doc1 = _ingest(db_session, vault, sample_case, f1, "a.txt")

    with pytest.raises(VersionLinkError):
        link_as_new_version(db_session, doc1, doc1, actor="test-user")


def test_link_as_new_version_rejects_document_already_in_a_group(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    f1, f2, f3 = (tmp_path / f"{i}.txt" for i in (1, 2, 3))
    for f, text in zip((f1, f2, f3), ("a", "b", "c")):
        f.write_text(text)

    doc1 = _ingest(db_session, vault, sample_case, f1, "1.txt")
    doc2 = _ingest(db_session, vault, sample_case, f2, "2.txt")
    link_as_new_version(db_session, doc1, doc2, actor="test-user")
    db_session.commit()

    doc3 = _ingest(db_session, vault, sample_case, f3, "3.txt")
    # doc2 is already grouped; linking it again as if it were "new" (rather
    # than linking a fresh document to it as the next version) is rejected.
    with pytest.raises(VersionLinkError):
        link_as_new_version(db_session, doc3, doc2, actor="test-user")


def test_link_as_new_version_rejects_linking_to_a_superseded_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    f1, f2, f3 = (tmp_path / f"{i}.txt" for i in (1, 2, 3))
    for f, text in zip((f1, f2, f3), ("a", "b", "c")):
        f.write_text(text)

    doc1 = _ingest(db_session, vault, sample_case, f1, "1.txt")
    doc2 = _ingest(db_session, vault, sample_case, f2, "2.txt")
    link_as_new_version(db_session, doc1, doc2, actor="test-user")
    db_session.commit()

    doc3 = _ingest(db_session, vault, sample_case, f3, "3.txt")
    # doc1 is no longer current -- new versions must chain off the current one.
    with pytest.raises(VersionLinkError):
        link_as_new_version(db_session, doc1, doc3, actor="test-user")


def test_database_rejects_two_current_versions_in_same_group(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """The partial unique index is a real DB-level guarantee, not just app discipline."""
    f1, f2 = tmp_path / "a.txt", tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")

    doc1 = _ingest(db_session, vault, sample_case, f1, "a.txt")
    doc2 = _ingest(db_session, vault, sample_case, f2, "b.txt")
    link_as_new_version(db_session, doc1, doc2, actor="test-user")
    db_session.commit()

    # Bypass the service layer and try to directly force a second "current"
    # document into the same group -- this must fail at the DB level even
    # if application code has a bug.
    doc1.is_current_version = True
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
