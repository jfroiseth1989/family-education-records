"""Tests for app/core/document_dates.py -- setting a document's own date."""

from __future__ import annotations

from datetime import date, timezone

from sqlalchemy.orm import Session

from app.core.document_dates import set_document_date
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentDatePrecision, DocumentDateSource


def _blank_document() -> Document:
    # A bare, unpersisted Document is enough to exercise the pure
    # attribute-setting logic in set_document_date() without touching the DB.
    return Document(
        case_id=1,
        original_filename="x.txt",
        stored_path="cases/1-x/originals/abc/x.txt",
        sha256_hash="0" * 64,
        file_size_bytes=1,
        ingested_by="test-user",
    )


def test_set_document_date_stores_exact_manual_date():
    document = _blank_document()

    set_document_date(
        document, date(2024, 3, 15), source=DocumentDateSource.MANUAL
    )

    assert document.document_date is not None
    assert document.document_date.date() == date(2024, 3, 15)
    assert document.document_date.tzinfo == timezone.utc
    assert document.document_date_source == "manual"
    assert document.document_date_precision == "exact"


def test_set_document_date_records_approximate_precision():
    document = _blank_document()

    set_document_date(
        document,
        date(2024, 3, 1),
        source=DocumentDateSource.MANUAL,
        precision=DocumentDatePrecision.APPROXIMATE,
    )

    assert document.document_date_precision == "approximate"


def test_set_document_date_none_clears_date_source_and_precision():
    document = _blank_document()
    set_document_date(document, date(2024, 1, 1), source=DocumentDateSource.MANUAL)

    set_document_date(document, None, source=DocumentDateSource.MANUAL)

    assert document.document_date is None
    assert document.document_date_source is None
    assert document.document_date_precision is None


def test_set_document_date_records_non_manual_sources_for_future_phases():
    """Not used anywhere yet in Phase 1, but the mechanism must not be manual-only."""
    document = _blank_document()

    set_document_date(document, date(2024, 5, 1), source=DocumentDateSource.EXTRACTED)
    assert document.document_date_source == "extracted"

    set_document_date(document, date(2024, 5, 1), source=DocumentDateSource.FILE_METADATA)
    assert document.document_date_source == "file_metadata"


def test_ingest_document_without_date_leaves_it_unknown(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file
):
    """Allowing unknown/unavailable dates is the default, not an edge case."""
    document = ingest_document(
        db_session, vault, sample_case, source_file, "iep.txt", actor="test-user"
    )
    db_session.commit()

    assert document.document_date is None
    assert document.document_date_source is None
    assert document.document_date_precision is None


def test_ingest_document_with_date_records_manual_source(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file
):
    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file,
        "iep.txt",
        actor="test-user",
        document_date=date(2023, 9, 1),
    )
    db_session.commit()

    assert document.document_date.date() == date(2023, 9, 1)
    assert document.document_date_source == "manual"
    assert document.document_date_precision == "exact"


def test_document_date_is_independent_of_filesystem_metadata(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path
):
    """The document's own date must never be derived from the source file's
    OS timestamps -- ingesting a file with an old mtime, with no date
    entered, must still leave document_date unset rather than backfilling
    it from the filesystem.
    """
    import os
    import time

    old_file = tmp_path / "old-file.txt"
    old_file.write_text("some content")
    old_timestamp = time.mktime((2010, 1, 1, 0, 0, 0, 0, 0, 0))
    os.utime(old_file, (old_timestamp, old_timestamp))

    document = ingest_document(
        db_session, vault, sample_case, old_file, "old-file.txt", actor="test-user"
    )
    db_session.commit()

    # No date was entered, so none should have been inferred from the
    # file's decade-old modification time.
    assert document.document_date is None
    assert document.ingested_at is not None  # ingestion timestamp is unaffected/unrelated
