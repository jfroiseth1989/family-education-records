"""Deterministic, local-only educational-record detection for
communication attachments (Communications Phase Step 5).

Reuses `app/core/document_type_suggestion.py` -- the same matcher already
used for the Document drop-zone auto-fill -- rather than a separate
classifier, per docs/COMMUNICATIONS_PLAN.md §1 decision 2. No AI/LLM, no
cloud service, no network call of any kind (same standing guarantee as
that module).

An attachment's best-effort *native* text (never OCR -- OCR is a
background job, and running it synchronously during email import would
turn "import a batch of emails" into a slow, blocking operation) is
extracted the same way the pre-ingestion Document preview does
(`app/api/documents.py::_native_text_sample`), duplicated here in small
form rather than imported from `app.api`, since `app.core` must not
depend on the API layer. Any extraction failure -- an unsupported
format, a corrupt/unreadable file -- is swallowed and treated as "no text
sample," never raised: classification is advisory only, so a file this
module can't read simply gets no suggestion and stays attached to its
Communication exactly as before, per the plan's "fail safely" requirement.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.document_type_suggestion import DocumentTypeSuggestion, suggest_document_type
from app.core.extraction.dispatcher import get_extractor, is_image_extension
from app.db.models import DocumentType

_MAX_PAGES_SAMPLED = 3


def _native_text_sample(filename: str, stored_path: Path) -> str:
    """Best-effort, OCR-free text sample for an already-stored attachment.

    Mirrors `app/api/documents.py::_native_text_sample` for a file that's
    already on disk (an attachment is always stored before classification
    runs -- see `app/core/communications/ingestion.py::_store_attachment`)
    rather than a not-yet-ingested upload. Never raises.
    """
    if is_image_extension(filename):
        return ""
    extractor = get_extractor(filename)
    if extractor is None:
        return ""
    try:
        result = extractor(stored_path)
    except Exception:
        return ""
    return "\n".join(page.text or "" for page in result.pages[:_MAX_PAGES_SAMPLED])


def classify_attachment(filename: str, stored_path: Path) -> DocumentTypeSuggestion | None:
    """Suggest a document type for one stored attachment, or None.

    None covers three cases the caller doesn't need to distinguish: no
    trigger matched, the match was ambiguous (several types matched), or
    the file's format/content couldn't be read at all -- in every case,
    the attachment simply stays an unclassified candidate rather than
    receiving a forced or guessed classification.
    """
    text_sample = _native_text_sample(filename, stored_path)
    return suggest_document_type(filename, text_sample)


def resolve_document_type_id(db: Session, suggestion: DocumentTypeSuggestion | None) -> int | None:
    """Resolve an already-computed type suggestion to a concrete
    `DocumentType.type_id`, or None if there's no suggestion or (should
    never happen -- every trigger name matches a seeded type exactly) it
    doesn't resolve to a real row.
    """
    if suggestion is None:
        return None
    return db.scalars(select(DocumentType.type_id).where(DocumentType.name == suggestion.type_name)).first()
