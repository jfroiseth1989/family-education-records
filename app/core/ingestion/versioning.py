"""Linking documents together as versions of the same logical record.

Versioning is always a relationship between two already-immutable
`Document` rows — never an edit to either one. A corrected record or a
reissued IEP is ingested as an ordinary new document (its own hash, its
own file, its own row) via `app.core.ingestion.service.ingest_document`,
and *then* explicitly linked here to the document it replaces. See
docs/ARCHITECTURE.md §3.2 and docs/DATA_MODEL.md "document_version_groups".
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.db.models import Document, DocumentVersionGroup


class VersionLinkError(Exception):
    """Raised when two documents can't be linked as versions of each other."""


def link_as_new_version(
    db: Session,
    existing_document: Document,
    new_document: Document,
    actor: str,
    version_note: str | None = None,
) -> DocumentVersionGroup:
    """Mark ``new_document`` as a newer version of ``existing_document``.

    Creates a :class:`DocumentVersionGroup` on first use (or reuses the one
    ``existing_document`` already belongs to), links the two documents via
    ``supersedes_document_id``, flips ``is_current_version`` so exactly one
    document per group is current, and records a custody event on each
    document. All of this happens in the caller's transaction — nothing
    here commits.

    Every previous version stays exactly as it was: this function never
    modifies a document's file, hash, or any of its other content fields —
    only the version-relationship columns and custody log.
    """
    if existing_document.case_id != new_document.case_id:
        raise VersionLinkError(
            "Documents must belong to the same case to be linked as versions."
        )
    if existing_document.document_id == new_document.document_id:
        raise VersionLinkError("A document cannot be linked as a new version of itself.")
    if new_document.version_group_id is not None:
        raise VersionLinkError("The new document is already part of a version group.")
    if not existing_document.is_current_version:
        raise VersionLinkError(
            "Only the current version of a document can have a new version linked to it. "
            "Link to the current version instead."
        )

    group = existing_document.version_group
    if group is None:
        group = DocumentVersionGroup(
            case_id=existing_document.case_id,
            label=existing_document.original_filename,
        )
        db.add(group)
        db.flush()  # assigns group.group_id
        existing_document.version_group_id = group.group_id
        existing_document.version_number = 1

    # The session is configured with autoflush=False (see
    # app/db/session.py), so the group-membership change just made above
    # would not otherwise be visible to the COUNT query below — flush
    # explicitly rather than relying on autoflush behavior.
    db.flush()

    next_version_number = (
        db.execute(
            select(func.count(Document.document_id)).where(
                Document.version_group_id == group.group_id
            )
        ).scalar_one()
        + 1
    )

    existing_document.is_current_version = False
    new_document.version_group_id = group.group_id
    new_document.version_number = next_version_number
    new_document.supersedes_document_id = existing_document.document_id
    new_document.is_current_version = True
    new_document.version_note = version_note

    # Kept in sync by application logic, in this same transaction — see
    # DocumentVersionGroup's docstring for why this isn't a DB-level FK.
    group.current_document_id = new_document.document_id

    write_custody_event(
        db,
        existing_document,
        event_type="version_superseded",
        actor=actor,
        details={"superseded_by_document_id": new_document.document_id},
    )
    write_custody_event(
        db,
        new_document,
        event_type="version_linked",
        actor=actor,
        details={
            "supersedes_document_id": existing_document.document_id,
            "version_note": version_note,
        },
    )

    return group
