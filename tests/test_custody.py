"""Tests for app/core/custody.py -- the chain-of-custody ledger and
integrity verification.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.custody import verify_document_integrity
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case


def test_verify_document_integrity_matches_for_untouched_file(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = ingest_document(
        db_session, vault, sample_case, source_file, "iep.txt", actor="test-user"
    )
    db_session.commit()

    result = verify_document_integrity(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert result is True
    latest_event = document.custody_events[-1]
    assert latest_event.event_type == "hash_verified"
    assert latest_event.details == {
        "recomputed_hash": document.sha256_hash,
        "matches": True,
    }


def test_verify_document_integrity_detects_tampering(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = ingest_document(
        db_session, vault, sample_case, source_file, "iep.txt", actor="test-user"
    )
    db_session.commit()
    recorded_hash = document.sha256_hash

    # Simulate someone bypassing the read-only protection and editing the
    # stored file directly -- the whole point of recording the hash is to
    # make this detectable.
    stored_path = vault.root / document.stored_path
    os.chmod(stored_path, stat.S_IWUSR | stat.S_IRUSR)
    stored_path.write_text("tampered content")

    result = verify_document_integrity(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert result is False
    # The document's recorded hash is untouched -- tampering is detected by
    # comparison, not by silently updating what we consider "correct".
    assert document.sha256_hash == recorded_hash
    latest_event = document.custody_events[-1]
    assert latest_event.event_type == "hash_verified"
    assert latest_event.details["matches"] is False
    assert latest_event.details["recomputed_hash"] != recorded_hash


def test_every_custody_event_has_required_fields(
    db_session: Session, vault: VaultLayout, sample_case: Case, source_file: Path
):
    document = ingest_document(
        db_session, vault, sample_case, source_file, "iep.txt", actor="test-user"
    )
    db_session.commit()

    for event in document.custody_events:
        assert event.original_filename
        assert event.sha256_hash_at_event
        assert event.file_size_bytes_at_event > 0
        assert event.storage_location_at_event
        assert event.actor
        assert event.event_timestamp is not None
