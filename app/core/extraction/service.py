"""Extraction orchestration: turning a stored original into `document_pages`.

`extract_document()` is the single entry point the API layer calls right
after ingestion succeeds (docs/PHASE_2_PLAN.md §5.1). It never fails the
caller's request — an unparseable or unsupported file is recorded via
`documents.extraction_status`/`extraction_error`, not raised as an
exception, so ingestion's guarantees never depend on extraction
succeeding (approved decision 6). It only ever opens a stored original in
read mode — see the per-format extractor modules — so a document's file
and hash are exactly as they were before extraction ran; that is a hard
guarantee, verified by tests/test_extraction_service.py.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.core.extraction.dispatcher import get_extractor, is_image_extension
from app.core.extraction.types import ExtractedAttachment, ExtractedPage
from app.core.ingestion.service import DuplicateDocumentError, ingest_document
from app.core.vault import VaultLayout
from app.db.models import Document, DocumentPage

# Caps recursive extraction of nested email attachments (an attachment
# that is itself an email with its own attachments, and so on) so a
# maliciously or accidentally deeply nested message can't hang a request.
# The attachment is still safely ingested at any depth -- only automatic
# recursive extraction stops.
_MAX_ATTACHMENT_DEPTH = 5


def extract_document(
    db: Session,
    vault: VaultLayout,
    document: Document,
    actor: str,
    *,
    _depth: int = 0,
) -> None:
    """Extract text from `document`'s stored file and populate `document_pages`.

    Idempotent: re-running this on a document that was already extracted
    replaces its `document_pages` rows rather than duplicating them, and
    logs a `re_extracted` custody event instead of `extracted` -- see
    docs/PHASE_2_PLAN.md §5.5. Does not commit; the caller controls the
    transaction boundary, same convention as ingest_document().
    """
    is_reextraction = document.extraction_status != "pending"
    full_path = vault.root / document.stored_path

    if is_image_extension(document.original_filename):
        _replace_pages(
            db,
            document,
            [
                ExtractedPage(
                    page_number=1, text=None, extraction_method="none",
                    char_count=0, needs_ocr=True,
                )
            ],
        )
        document.page_count = 1
        document.has_text_layer = False
        document.needs_ocr = True
        document.extraction_status = "completed"
        document.extraction_error = None
        _log_extraction_event(
            db, document, actor, is_reextraction,
            details={"reason": "image_format_no_native_text"},
        )
        return

    extractor = get_extractor(document.original_filename)
    if extractor is None:
        _replace_pages(db, document, [])
        document.page_count = 0
        document.has_text_layer = False
        document.needs_ocr = False
        document.extraction_status = "unsupported_format"
        document.extraction_error = None
        _log_extraction_event(
            db, document, actor, is_reextraction,
            details={"reason": "unsupported_format"},
        )
        return

    try:
        result = extractor(full_path)
    except Exception as exc:  # noqa: BLE001 -- any extractor failure must not fail ingestion
        _replace_pages(db, document, [])
        document.page_count = None
        document.has_text_layer = None
        document.needs_ocr = False
        document.extraction_status = "failed"
        document.extraction_error = f"{type(exc).__name__}: {exc}"[:500]
        _log_extraction_event(
            db, document, actor, is_reextraction,
            event_type="extraction_failed",
            details={"error": document.extraction_error},
        )
        return

    _replace_pages(db, document, result.pages)
    document.page_count = len(result.pages)
    document.has_text_layer = any(
        page.extraction_method == "native" and page.char_count > 0 for page in result.pages
    )
    document.needs_ocr = any(page.needs_ocr for page in result.pages)
    document.extraction_status = "completed"
    document.extraction_error = None
    _log_extraction_event(
        db, document, actor, is_reextraction,
        details={"page_count": document.page_count},
    )

    if _depth < _MAX_ATTACHMENT_DEPTH:
        for attachment in result.attachments:
            _ingest_and_extract_attachment(db, vault, document, attachment, actor, _depth + 1)


def _replace_pages(db: Session, document: Document, pages: list[ExtractedPage]) -> None:
    """Delete any existing pages for `document` and insert `pages` in their place.

    This is what makes extract_document() idempotent -- a re-run replaces
    rather than duplicates.
    """
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.document_id))
    for page in pages:
        db.add(
            DocumentPage(
                document_id=document.document_id,
                page_number=page.page_number,
                extracted_text=page.text,
                extraction_method=page.extraction_method,
                char_count=page.char_count,
                needs_ocr=page.needs_ocr,
                # Snapshot of the parent's current hash -- see
                # docs/PHASE_2_PLAN.md §12.2. documents.sha256_hash never
                # changes after creation, so this should always match it.
                source_sha256=document.sha256_hash,
            )
        )


def _log_extraction_event(
    db: Session,
    document: Document,
    actor: str,
    is_reextraction: bool,
    *,
    event_type: str | None = None,
    details: dict | None = None,
) -> None:
    if event_type is None:
        event_type = "re_extracted" if is_reextraction else "extracted"
    write_custody_event(db, document, event_type=event_type, actor=actor, details=details)


def _ingest_and_extract_attachment(
    db: Session,
    vault: VaultLayout,
    parent_document: Document,
    attachment: ExtractedAttachment,
    actor: str,
    depth: int,
) -> None:
    """Ingest one email attachment as its own document and extract it too.

    A byte-identical duplicate (the same file attached elsewhere in this
    case) is skipped rather than treated as an error -- it's already a
    fully ingested, extracted document under its own row; re-attaching it
    doesn't need a second copy.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / attachment.filename
    try:
        tmp_path.write_bytes(attachment.content)
        try:
            child = ingest_document(
                db,
                vault,
                parent_document.case,
                source_file_path=tmp_path,
                original_filename=attachment.filename,
                actor=actor,
                source=f"Email attachment of document #{parent_document.document_id}",
                mime_type=attachment.mime_type,
            )
        except DuplicateDocumentError:
            return
    finally:
        tmp_path.unlink(missing_ok=True)
        tmp_dir.rmdir()

    extract_document(db, vault, child, actor, _depth=depth)
