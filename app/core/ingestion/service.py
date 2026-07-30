"""Document ingestion: bringing a file into the vault under case management.

Ingesting a document is the *only* way a file enters `originals/`. It
always copies (never moves or links) the source file, computes and records
its SHA-256 hash, sets the stored copy read-only, and writes the
document's first chain-of-custody event in the same database transaction
as the document row itself — see docs/ARCHITECTURE.md §3.1.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.core.files import compute_sha256, copy_into_vault, make_read_only
from app.core.vault import VaultLayout
from app.db.models import Case, Document


class DuplicateDocumentError(Exception):
    """Raised when a file with an identical SHA-256 hash already exists in this case.

    Exact duplicates are flagged, never silently discarded or silently
    re-imported as a second copy — see docs/ARCHITECTURE.md §3.1.
    """

    def __init__(self, existing_document: Document):
        self.existing_document = existing_document
        super().__init__(
            "A document with identical content already exists in this case "
            f"(document_id={existing_document.document_id}, imported "
            f"{existing_document.ingested_at}, as "
            f"'{existing_document.original_filename}')."
        )


def ingest_document(
    db: Session,
    vault: VaultLayout,
    case: Case,
    source_file_path: Path,
    original_filename: str,
    actor: str,
    source: str | None = None,
    document_type_id: int | None = None,
    notes: str | None = None,
    mime_type: str | None = None,
) -> Document:
    """Copy ``source_file_path`` into the vault and register it as a new document.

    Never modifies or deletes ``source_file_path`` — it is opened for
    reading only. Raises :class:`DuplicateDocumentError` (without ingesting
    anything) if a document with the same SHA-256 hash already exists in
    this case.

    Does not commit; the caller controls the transaction boundary (this
    lets API layers batch a request into a single commit).
    """
    file_hash = compute_sha256(source_file_path)
    file_size = source_file_path.stat().st_size

    existing = db.execute(
        select(Document).where(
            Document.case_id == case.case_id,
            Document.sha256_hash == file_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateDocumentError(existing)

    destination = vault.originals_dir(case.case_id, case.label) / file_hash / original_filename
    copy_into_vault(source_file_path, destination)
    make_read_only(destination)

    relative_stored_path = str(destination.relative_to(vault.root))

    document = Document(
        case_id=case.case_id,
        original_filename=original_filename,
        stored_path=relative_stored_path,
        sha256_hash=file_hash,
        mime_type=mime_type,
        file_size_bytes=file_size,
        source=source,
        document_type_id=document_type_id,
        ingested_by=actor,
        notes=notes,
    )
    db.add(document)
    db.flush()  # assigns document.document_id before the custody event references it

    write_custody_event(db, document, event_type="imported", actor=actor)

    return document
