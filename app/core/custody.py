"""Chain-of-custody ledger writes and integrity verification.

Every action taken on a document — import, version linking, a manual
integrity check, and (in later phases) extraction, OCR, tagging, export
inclusion — appends one row to `document_custody_events`. This module is
the only place in the application that constructs those rows, so the
"what fields does a custody event always carry" invariant lives in one
place. See docs/DATA_MODEL.md "document_custody_events" and
docs/PRIVACY_SECURITY.md §3-4.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import Document, DocumentCustodyEvent


def write_custody_event(
    db: Session,
    document: Document,
    event_type: str,
    actor: str,
    details: dict | None = None,
) -> DocumentCustodyEvent:
    """Append a custody event for ``document``.

    Snapshots the document's *current* filename/hash/size/location onto
    the event at the moment it's written, rather than only recording those
    fields once at import — so the ledger itself shows integrity (or a
    discrepancy) over the document's whole history, not just at import
    time. Does not commit; the caller controls the transaction boundary.
    """
    event = DocumentCustodyEvent(
        document=document,
        event_type=event_type,
        actor=actor,
        original_filename=document.original_filename,
        sha256_hash_at_event=document.sha256_hash,
        file_size_bytes_at_event=document.file_size_bytes,
        storage_location_at_event=document.stored_path,
        details=details,
    )
    db.add(event)
    return event


def verify_document_integrity(
    db: Session,
    vault: VaultLayout,
    document: Document,
    actor: str,
) -> bool:
    """Recompute a document's stored-file hash and compare it to the recorded hash.

    Always logs a ``hash_verified`` custody event with the freshly
    recomputed hash and whether it matched — including when it doesn't —
    because the ledger should reflect what was actually observed at this
    moment, not just assert a boolean with no evidence behind it.

    Returns True if the file on disk still matches its recorded hash.
    """
    full_path = vault.root / document.stored_path
    recomputed_hash = compute_sha256(Path(full_path))
    matches = recomputed_hash == document.sha256_hash
    write_custody_event(
        db,
        document,
        event_type="hash_verified",
        actor=actor,
        details={"recomputed_hash": recomputed_hash, "matches": matches},
    )
    return matches
