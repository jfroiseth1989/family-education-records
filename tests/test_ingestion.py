"""Tests for app/core/ingestion/service.py -- the sole entry point for
bringing a file into the vault.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.files import compute_sha256
from app.core.ingestion.service import DuplicateDocumentError, ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentCustodyEvent


def test_ingest_document_copies_file_and_records_hash(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    original_hash = compute_sha256(source_file)

    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
        source="parent's own scan",
    )
    db_session.commit()

    assert document.document_id is not None
    assert document.sha256_hash == original_hash
    assert document.original_filename == "iep-2024.txt"
    assert document.case_id == sample_case.case_id

    stored_path = vault.root / document.stored_path
    assert stored_path.exists()
    assert stored_path.read_text() == source_file.read_text()


def test_ingest_document_never_modifies_the_source_file(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    original_content = source_file.read_text()
    original_hash = compute_sha256(source_file)

    ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
    )
    db_session.commit()

    assert source_file.read_text() == original_content
    assert compute_sha256(source_file) == original_hash


def test_ingested_file_is_read_only_in_the_vault(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    import os
    import stat

    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
    )
    db_session.commit()

    stored_path = vault.root / document.stored_path
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    assert stored_path.stat().st_mode & write_bits == 0


def test_ingest_document_writes_imported_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
    )
    db_session.commit()

    events = db_session.scalars(
        select(DocumentCustodyEvent).where(DocumentCustodyEvent.document_id == document.document_id)
    ).all()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "imported"
    assert event.actor == "test-user"
    assert event.sha256_hash_at_event == document.sha256_hash
    assert event.file_size_bytes_at_event == document.file_size_bytes
    assert event.storage_location_at_event == document.stored_path
    assert event.original_filename == "iep-2024.txt"


def test_ingest_document_rejects_exact_duplicate_in_same_case(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    first = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
    )
    db_session.commit()

    # Re-ingesting the exact same bytes (even under a different filename)
    # must be flagged, not silently duplicated -- see docs/ARCHITECTURE.md §3.1.
    with pytest.raises(DuplicateDocumentError) as excinfo:
        ingest_document(
            db_session,
            vault,
            sample_case,
            source_file_path=source_file,
            original_filename="iep-2024-copy.txt",
            actor="test-user",
        )
    assert excinfo.value.existing_document.document_id == first.document_id

    # And no second document row or file should have been created.
    all_documents = db_session.scalars(
        select(Document).where(Document.case_id == sample_case.case_id)
    ).all()
    assert len(all_documents) == 1


def test_ingest_document_allows_same_content_in_different_cases(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    other_case = Case(label="A different case")
    db_session.add(other_case)
    db_session.commit()

    doc1 = ingest_document(
        db_session, vault, sample_case, source_file, "iep.txt", actor="test-user"
    )
    doc2 = ingest_document(
        db_session, vault, other_case, source_file, "iep.txt", actor="test-user"
    )
    db_session.commit()

    assert doc1.document_id != doc2.document_id
    assert doc1.sha256_hash == doc2.sha256_hash
    assert doc1.case_id != doc2.case_id


def test_ingest_document_records_date_received_when_given(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
        date_received=date(2024, 3, 15),
    )
    db_session.commit()

    assert document.date_received is not None
    assert document.date_received.date() == date(2024, 3, 15)


def test_ingest_document_leaves_date_received_unset_by_default(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    """Every pre-Step-4 caller of ingest_document (the entire rest of this
    test suite) never passes date_received -- it must default to None,
    the "not recorded" state, not error or guess.
    """
    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file_path=source_file,
        original_filename="iep-2024.txt",
        actor="test-user",
    )
    db_session.commit()

    assert document.date_received is None
