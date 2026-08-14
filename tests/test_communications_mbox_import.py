"""Tests for app/core/communications/mbox_import.py -- manual .mbox
archive import (Communications Phase Step 8).
"""

from __future__ import annotations

import stat
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.mbox_import import import_mbox_file
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import (
    AiObservation,
    Case,
    Communication,
    CommunicationAttachment,
    CommunicationCustodyEvent,
)


def _message(
    *,
    message_id: str,
    subject: str = "IEP Meeting Notice",
    from_addr: str = "amanda.wagner@district.example.org",
    date: str = "Mon, 7 Mar 2022 14:30:00 -0500",
    body: str = "Please see the attached notice.",
) -> str:
    return (
        f"From: Amanda Wagner <{from_addr}>\n"
        f"To: parent@yahoo.com\n"
        f"Subject: {subject}\n"
        f"Date: {date}\n"
        f"Message-ID: {message_id}\n"
        f"\n"
        f"{body}\n"
    )


def _write_mbox(tmp_path: Path, messages: list[str], name: str = "archive.mbox") -> Path:
    """Build a real mbox file: each message preceded by its own "From "
    envelope separator line, exactly the format Python's stdlib
    `mailbox.mbox` (and every other mbox reader) expects."""
    path = tmp_path / "source-files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for msg in messages:
        parts.append(f"From sender@example.org Mon Mar  7 14:30:00 2022\n{msg}\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def test_import_splits_archive_into_separate_communications(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(
        tmp_path,
        [
            _message(message_id="<one@example.org>", subject="First Notice"),
            _message(message_id="<two@example.org>", subject="Second Notice"),
        ],
    )

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    assert len(result.imported) == 2
    assert result.duplicate_count == 0
    assert db_session.query(Communication).count() == 2
    subjects = {c.subject for c in result.imported}
    assert subjects == {"First Notice", "Second Notice"}


def test_imported_messages_record_mbox_import_method_and_custody_details(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])
    archive_hash = compute_sha256(archive)

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    communication = result.imported[0]
    assert communication.import_method == "mbox_import"

    event = db_session.scalars(
        select(CommunicationCustodyEvent).where(
            CommunicationCustodyEvent.communication_id == communication.communication_id
        )
    ).first()
    assert event is not None
    assert event.details["source_archive_filename"] == "archive.mbox"
    assert event.details["source_archive_sha256"] == archive_hash


def test_each_extracted_message_is_stored_read_only_and_hashed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    communication = result.imported[0]
    stored_path = vault.root / communication.stored_path
    assert stored_path.exists()
    assert compute_sha256(stored_path) == communication.sha256_hash

    mode = stored_path.stat().st_mode
    assert not (mode & stat.S_IWUSR)


def test_import_never_modifies_the_source_archive(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])
    original_content = archive.read_bytes()
    original_hash = compute_sha256(archive)

    import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    assert archive.read_bytes() == original_content
    assert compute_sha256(archive) == original_hash


def test_duplicate_message_within_the_same_archive_is_skipped_not_aborted(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(
        tmp_path,
        [
            _message(message_id="<dup@example.org>", subject="First"),
            _message(message_id="<dup@example.org>", subject="First"),
        ],
    )

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    assert len(result.imported) == 1
    assert result.duplicate_count == 1
    assert db_session.query(Communication).count() == 1


def test_reimporting_the_same_archive_skips_all_messages_as_duplicates(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])

    import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    assert len(result.imported) == 0
    assert result.duplicate_count == 1
    assert db_session.query(Communication).count() == 1


def test_attachment_classification_still_works_for_mbox_messages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    body = (
        "From: sender@example.org\n"
        "To: parent@yahoo.com\n"
        "Subject: Has an attachment\n"
        "Message-ID: <att-1@example.org>\n"
        'Content-Type: multipart/mixed; boundary="BOUNDARY"\n'
        "\n"
        "--BOUNDARY\n"
        "Content-Type: text/plain\n\n"
        "See attached.\n"
        "--BOUNDARY\n"
        "Content-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="2023 Annual IEP.pdf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        "JVBERi0xLjQK\n"
        "--BOUNDARY--\n"
    )
    archive = _write_mbox(tmp_path, [body])

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    communication = result.imported[0]
    attachments = db_session.scalars(
        select(CommunicationAttachment).where(
            CommunicationAttachment.communication_id == communication.communication_id
        )
    ).all()
    assert len(attachments) == 1
    assert attachments[0].filename == "2023 Annual IEP.pdf"


def test_timeline_suggestion_still_generated_for_mbox_messages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    communication = result.imported[0]
    observation = db_session.scalars(
        select(AiObservation).where(AiObservation.communication_id == communication.communication_id)
    ).first()
    assert observation is not None
    assert observation.status == "pending_review"


def test_empty_entries_are_skipped_and_counted(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    archive = _write_mbox(tmp_path, [_message(message_id="<one@example.org>")])
    # Append a stray envelope separator with no actual message content --
    # a real-world mbox oddity (e.g. a trailing blank "From " line from a
    # buggy exporter) that must not crash the import.
    with archive.open("a", encoding="utf-8") as f:
        f.write("From nobody Mon Mar  7 14:30:00 2022\n")

    result = import_mbox_file(
        db_session, vault, sample_case, source_file_path=archive, original_filename="archive.mbox", actor="test-user"
    )
    db_session.commit()

    assert len(result.imported) == 1
    assert result.total_messages == 2
