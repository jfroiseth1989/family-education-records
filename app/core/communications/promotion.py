"""Promoting a communication attachment to a Document (Communications
Phase Step 5).

Reuses the existing Document ingestion path (`ingest_document()`)
unchanged -- this module never copies bytes, computes a hash, or writes
to the vault itself; `ingest_document()` still does all of that, reading
the attachment's own already-preserved, read-only stored copy as its
source. The attachment file is never modified, moved, or deleted by any
function here, promoted or not -- see
`app/core/communications/ingestion.py` for where it was originally
stored.

Duplicate handling is the core of this module (docs/COMMUNICATIONS_PLAN.md
§8): if the attachment's content already matches a `Document` in the
target case, `ingest_document()` raises `DuplicateDocumentError` rather
than creating a second copy. That is not a failure here -- it is caught,
and the existing document is linked instead, via a `CommunicationDocumentLink`
row that records this specific email/attachment's independent provenance
without duplicating the underlying `Document`. Re-running the same
promotion (e.g. a double form submit) is idempotent: a second attempt
finds the link row already in place and does nothing further -- no
duplicate link row, no duplicate custody event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.custody import write_communication_custody_event
from app.core.custody import write_custody_event
from app.core.ingestion.service import DuplicateDocumentError, ingest_document
from app.core.vault import VaultLayout
from app.db.models import CommunicationAttachment, CommunicationDocumentLink, Document


class AttachmentNotPromotableError(Exception):
    """Raised when an attachment can't be promoted at all -- its stored
    file is missing from the vault, or its Communication has no assigned
    case (manual upload always requires one, so this should not happen
    in practice, but promotion never assumes it).
    """


@dataclass(frozen=True)
class PromotionResult:
    document: Document
    created_new_document: bool
    already_linked: bool


def promote_attachment_to_document(
    db: Session,
    vault: VaultLayout,
    attachment: CommunicationAttachment,
    *,
    document_type_id: int | None,
    source: str | None,
    notes: str | None,
    date_received: date | None,
    field_provenance: dict[str, str] | None,
    actor: str,
) -> PromotionResult:
    """Add `attachment` to Documents, reusing an existing Document by
    content hash instead of creating a duplicate. Does not commit; the
    caller controls the transaction boundary.
    """
    communication = attachment.communication
    case = communication.case
    if case is None:
        raise AttachmentNotPromotableError(
            f"Communication {communication.communication_id} has no assigned student."
        )

    stored_path = vault.root / attachment.stored_path
    if not stored_path.exists():
        raise AttachmentNotPromotableError(
            f"Attachment {attachment.attachment_id}'s stored file is missing from the vault."
        )

    try:
        document = ingest_document(
            db,
            vault,
            case,
            source_file_path=stored_path,
            original_filename=attachment.filename,
            actor=actor,
            source=source,
            document_type_id=document_type_id,
            notes=notes,
            mime_type=attachment.mime_type,
            date_received=date_received,
            field_provenance=field_provenance,
        )
        created_new_document = True
    except DuplicateDocumentError as exc:
        document = exc.existing_document
        created_new_document = False

    existing_link = db.scalars(
        select(CommunicationDocumentLink).where(
            CommunicationDocumentLink.document_id == document.document_id,
            CommunicationDocumentLink.communication_id == attachment.communication_id,
            CommunicationDocumentLink.communication_attachment_id == attachment.attachment_id,
        )
    ).first()

    if existing_link is None:
        db.add(
            CommunicationDocumentLink(
                document_id=document.document_id,
                communication_id=attachment.communication_id,
                communication_attachment_id=attachment.attachment_id,
            )
        )
        write_custody_event(
            db,
            document,
            event_type="communication_attachment_linked",
            actor=actor,
            details={
                "communication_id": attachment.communication_id,
                "communication_attachment_id": attachment.attachment_id,
                "attachment_filename": attachment.filename,
                "email_subject": communication.subject,
                "email_from": communication.from_address,
                "reused_existing_document": not created_new_document,
            },
        )
        write_communication_custody_event(
            db,
            communication,
            event_type="attachment_added_to_documents",
            actor=actor,
            details={
                "communication_attachment_id": attachment.attachment_id,
                "document_id": document.document_id,
                "reused_existing_document": not created_new_document,
            },
        )

    attachment.resulting_document_id = document.document_id
    attachment.review_status = "added_to_documents"
    db.flush()

    return PromotionResult(
        document=document,
        created_new_document=created_new_document,
        already_linked=existing_link is not None,
    )


def exclude_attachment(db: Session, attachment: CommunicationAttachment, actor: str) -> None:
    """Mark `attachment` as reviewed and deliberately not added to
    Documents. Never touches the attachment's own preserved bytes/hash,
    and never creates or removes a Document. Idempotent: re-excluding an
    already-excluded attachment writes no new custody event.
    """
    if attachment.review_status == "excluded":
        return
    attachment.review_status = "excluded"
    write_communication_custody_event(
        db,
        attachment.communication,
        event_type="attachment_excluded",
        actor=actor,
        details={"communication_attachment_id": attachment.attachment_id, "filename": attachment.filename},
    )


def leave_attachment_with_email(db: Session, attachment: CommunicationAttachment, actor: str) -> None:
    """Mark `attachment` as reviewed and deliberately left as email-only
    (not promoted, not excluded -- just acknowledged and kept with the
    message it arrived on). Idempotent, same as `exclude_attachment`.
    """
    if attachment.review_status == "left_with_email":
        return
    attachment.review_status = "left_with_email"
    write_communication_custody_event(
        db,
        attachment.communication,
        event_type="attachment_left_with_email",
        actor=actor,
        details={"communication_attachment_id": attachment.attachment_id, "filename": attachment.filename},
    )
