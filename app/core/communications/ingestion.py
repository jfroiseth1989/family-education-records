"""Manual .eml import: bringing an email file into the vault under case
management (Communications Phase Step 3).

Independent of any connected mailbox -- docs/COMMUNICATIONS_PLAN.md §1
decision 3, "manual .eml upload is fully independent of Yahoo." This is
the only thing that makes that true in code: `account_id` is always
`None` for a communication created here; only IMAP-synced messages (a
later step) ever set it.

Mirrors app/core/ingestion/service.py's ingest_document() closely:
always copies (never moves) the source file, computes and records its
SHA-256 hash, sets the stored copy read-only, and writes the
communication's first custody event in the same transaction as the row
itself. Attachments get the identical treatment, one level down.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.attachment_classification import classify_attachment, resolve_document_type_id
from app.core.communications.custody import write_communication_custody_event
from app.core.communications.thread_rebuild import rebuild_threads
from app.core.communications.timeline_suggestions import generate_timeline_suggestion
from app.core.extraction.email import parse_message
from app.core.extraction.types import ExtractedAttachment
from app.core.files import compute_sha256, copy_into_vault, make_read_only
from app.core.vault import VaultLayout
from app.db.models import Case, Communication, CommunicationAttachment


class DuplicateCommunicationError(Exception):
    """Raised when a message with an identical SHA-256 hash (or the same
    Message-ID, for the same account) already exists.

    Mirrors `DuplicateDocumentError` -- an exact duplicate is flagged,
    never silently discarded or silently re-imported as a second copy.
    """

    def __init__(self, existing_communication: Communication):
        self.existing_communication = existing_communication
        super().__init__(
            "A message with identical content already exists in this case "
            f"(communication_id={existing_communication.communication_id}, imported "
            f"{existing_communication.imported_at})."
        )


def _find_duplicate(
    db: Session, account_id: int | None, message_id: str | None, file_hash: str
) -> Communication | None:
    """The dedup rule from docs/COMMUNICATIONS_PLAN.md §7: primarily by
    (account_id, message_id) when a Message-ID is present, falling back
    to (account_id, sha256_hash) when it isn't.

    Checked here in application logic -- not only relying on the
    schema's own partial unique index for the message-id case -- for two
    reasons: a clean `DuplicateCommunicationError` instead of a raw
    `IntegrityError` reaching the caller, and because the no-message-id
    fallback is not something the database enforces at all (see the
    `Communication` model's index docstring).
    """
    if message_id:
        existing = db.scalars(
            select(Communication).where(
                Communication.account_id == account_id,
                Communication.message_id_header == message_id,
            )
        ).first()
        if existing is not None:
            return existing

    return db.scalars(
        select(Communication).where(
            Communication.account_id == account_id,
            Communication.sha256_hash == file_hash,
        )
    ).first()


def import_eml_file(
    db: Session,
    vault: VaultLayout,
    case: Case,
    source_file_path: Path,
    original_filename: str,
    actor: str,
    import_method: str = "manual_upload",
    custody_details: dict | None = None,
) -> Communication:
    """Parse, hash, and copy `source_file_path` (a `.eml` file, or one
    message's raw RFC822 bytes extracted unmodified from an mbox archive
    -- see app/core/communications/mbox_import.py, Communications Phase
    Step 8) into the vault, registering it as a new `Communication` with
    its attachments.

    Never modifies or deletes `source_file_path` -- opened for reading
    only. Raises `DuplicateCommunicationError` (without importing
    anything) if an identical message already exists. `import_method`
    and `custody_details` are additive, optional overrides -- every
    pre-Step-8 caller leaves them at their defaults and sees no change
    in behavior. Does not commit; the caller controls the transaction
    boundary.
    """
    parsed = parse_message(source_file_path)
    file_hash = compute_sha256(source_file_path)
    file_size = source_file_path.stat().st_size

    existing = _find_duplicate(db, account_id=None, message_id=parsed.message_id, file_hash=file_hash)
    if existing is not None:
        raise DuplicateCommunicationError(existing)

    destination = vault.communications_dir(case.case_id, case.label) / file_hash / original_filename
    copy_into_vault(source_file_path, destination)
    make_read_only(destination)
    relative_stored_path = str(destination.relative_to(vault.root))

    communication = Communication(
        account_id=None,
        case_id=case.case_id,
        communication_type="email",
        subject=parsed.subject,
        from_address=parsed.from_address,
        from_display_name=parsed.from_display_name,
        to_addresses=parsed.to_addresses or None,
        cc_addresses=parsed.cc_addresses or None,
        bcc_addresses=parsed.bcc_addresses or None,
        sent_at=parsed.sent_at,
        message_id_header=parsed.message_id,
        in_reply_to_header=parsed.in_reply_to,
        references_header=parsed.references or None,
        raw_headers=parsed.raw_headers or None,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        sha256_hash=file_hash,
        stored_path=relative_stored_path,
        file_size_bytes=file_size,
        import_method=import_method,
        imported_by=actor,
    )
    db.add(communication)
    db.flush()  # assigns communication.communication_id

    write_communication_custody_event(db, communication, event_type="imported", actor=actor, details=custody_details)

    for attachment in parsed.attachments:
        _store_attachment(db, vault, case, communication, attachment)

    rebuild_threads(db)

    # Communications Phase Step 7: a deterministic, pending-review
    # timeline suggestion only -- never a verified fact or timeline
    # event on its own. `actor` is left at its system default (matching
    # app/core/facts/date_extraction.py's own convention: the *observation*
    # was produced by the deterministic generator, not by whichever human
    # happened to trigger this import -- reviewedby/reviewed_at are what
    # later records a real human's decision). Safe to call
    # unconditionally: produces nothing when the message has no reliable
    # date, and is idempotent by construction (see
    # generate_timeline_suggestion's docstring), so this can never
    # accumulate a duplicate even if this function is somehow invoked
    # twice for the same communication.
    generate_timeline_suggestion(db, communication)

    return communication


def _store_attachment(
    db: Session,
    vault: VaultLayout,
    case: Case,
    communication: Communication,
    attachment: ExtractedAttachment,
) -> CommunicationAttachment:
    """Write one parsed attachment's bytes into the vault, read-only, and
    record it -- always `review_status="pending"`; classification (Step 5)
    only ever fills in `is_educational_record_candidate`/
    `suggested_document_type_id` as advisory hints, never promotes
    anything to a `Document` or changes `review_status` itself.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(attachment.filename).suffix) as tmp:
        tmp.write(attachment.content)
        tmp_path = Path(tmp.name)
    try:
        attachment_hash = compute_sha256(tmp_path)
        destination = (
            vault.communications_dir(case.case_id, case.label)
            / communication.sha256_hash
            / "attachments"
            / attachment_hash
            / attachment.filename
        )
        copy_into_vault(tmp_path, destination)
        make_read_only(destination)
    finally:
        tmp_path.unlink(missing_ok=True)

    relative_stored_path = str(destination.relative_to(vault.root))

    # Classification reads the file back from its final, already-read-only
    # vault location -- read-only access, never a second write -- and is
    # never allowed to fail attachment storage: an unsupported or
    # unreadable format simply yields no suggestion (see
    # classify_attachment's docstring).
    type_suggestion = classify_attachment(attachment.filename, destination)
    suggested_type_id = resolve_document_type_id(db, type_suggestion)

    row = CommunicationAttachment(
        communication_id=communication.communication_id,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=len(attachment.content),
        sha256_hash=attachment_hash,
        stored_path=relative_stored_path,
        is_educational_record_candidate=suggested_type_id is not None,
        suggested_document_type_id=suggested_type_id,
    )
    db.add(row)
    db.flush()
    return row
