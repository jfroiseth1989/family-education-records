"""Tests for app/core/document_dates.py -- setting a document's own date."""

from __future__ import annotations

from datetime import date, timezone

import pytest
from sqlalchemy.orm import Session

from app.core.document_dates import (
    InvalidDateRangeError,
    set_document_date,
    validate_date_combination,
)
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


def test_set_document_date_stores_range():
    document = _blank_document()

    set_document_date(
        document,
        date(2024, 3, 1),
        source=DocumentDateSource.MANUAL,
        precision=DocumentDatePrecision.RANGE,
        range_end=date(2024, 3, 15),
    )

    assert document.document_date.date() == date(2024, 3, 1)
    assert document.document_date_range_end.date() == date(2024, 3, 15)
    assert document.document_date_range_end.tzinfo == timezone.utc
    assert document.document_date_precision == "range"


def test_set_document_date_none_clears_range_end_too():
    document = _blank_document()
    set_document_date(
        document,
        date(2024, 3, 1),
        source=DocumentDateSource.MANUAL,
        precision=DocumentDatePrecision.RANGE,
        range_end=date(2024, 3, 15),
    )

    set_document_date(document, None, source=DocumentDateSource.MANUAL)

    assert document.document_date is None
    assert document.document_date_range_end is None
    assert document.document_date_source is None
    assert document.document_date_precision is None


def test_set_document_date_range_without_end_raises():
    document = _blank_document()
    with pytest.raises(InvalidDateRangeError):
        set_document_date(
            document,
            date(2024, 3, 1),
            source=DocumentDateSource.MANUAL,
            precision=DocumentDatePrecision.RANGE,
        )


def test_set_document_date_range_end_before_start_raises():
    document = _blank_document()
    with pytest.raises(InvalidDateRangeError):
        set_document_date(
            document,
            date(2024, 3, 15),
            source=DocumentDateSource.MANUAL,
            precision=DocumentDatePrecision.RANGE,
            range_end=date(2024, 3, 1),
        )


def test_set_document_date_range_end_equal_to_start_is_allowed():
    """A one-day 'range' (start == end) is a degenerate but valid case --
    rejecting it would force the user into 'exact' for no real reason.
    """
    document = _blank_document()
    set_document_date(
        document,
        date(2024, 3, 1),
        source=DocumentDateSource.MANUAL,
        precision=DocumentDatePrecision.RANGE,
        range_end=date(2024, 3, 1),
    )
    assert document.document_date_range_end.date() == date(2024, 3, 1)


@pytest.mark.parametrize(
    "precision", [DocumentDatePrecision.EXACT, DocumentDatePrecision.APPROXIMATE]
)
def test_set_document_date_range_end_without_range_precision_raises(precision):
    document = _blank_document()
    with pytest.raises(InvalidDateRangeError):
        set_document_date(
            document,
            date(2024, 3, 1),
            source=DocumentDateSource.MANUAL,
            precision=precision,
            range_end=date(2024, 3, 15),
        )


def test_validate_date_combination_is_a_pure_no_op_on_success():
    """Must not raise, and must not require a Document instance to call."""
    validate_date_combination(None, DocumentDatePrecision.EXACT, None)
    validate_date_combination(date(2024, 1, 1), DocumentDatePrecision.EXACT, None)
    validate_date_combination(
        date(2024, 1, 1), DocumentDatePrecision.RANGE, date(2024, 1, 5)
    )


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


def test_ingest_document_with_range_date(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file
):
    document = ingest_document(
        db_session,
        vault,
        sample_case,
        source_file,
        "iep.txt",
        actor="test-user",
        document_date=date(2024, 3, 1),
        document_date_precision=DocumentDatePrecision.RANGE,
        document_date_range_end=date(2024, 3, 15),
    )
    db_session.commit()

    assert document.document_date.date() == date(2024, 3, 1)
    assert document.document_date_range_end.date() == date(2024, 3, 15)
    assert document.document_date_precision == "range"
    assert document.document_date_source == "manual"


def test_ingest_document_invalid_range_raises_before_touching_filesystem(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file
):
    """An invalid date combination must be rejected before the source file
    is copied into the vault -- otherwise a failed ingestion would leave an
    orphaned, read-only file on disk with no corresponding document row.
    """
    with pytest.raises(InvalidDateRangeError):
        ingest_document(
            db_session,
            vault,
            sample_case,
            source_file,
            "iep.txt",
            actor="test-user",
            document_date=date(2024, 3, 1),
            document_date_precision=DocumentDatePrecision.RANGE,
            # missing range_end -- invalid combination
        )

    # Nothing should have been written to the vault or the database.
    originals_dir = vault.originals_dir(sample_case.case_id, sample_case.label)
    stored_files = list(originals_dir.rglob("*")) if originals_dir.exists() else []
    assert stored_files == []

    from sqlalchemy import select

    documents = db_session.scalars(
        select(Document).where(Document.case_id == sample_case.case_id)
    ).all()
    assert documents == []
