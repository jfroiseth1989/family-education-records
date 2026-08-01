"""Annotations: highlights, notes, and bookmarks on a document.

See docs/DATA_MODEL.md "annotations" and docs/PHASE_2_PLAN.md §7/§13
Step 4. Every write here logs an `annotated`/`annotation_removed` custody
event on the document — same discipline as every other document action.

A highlight is the one annotation kind that also creates a `citations`
row. Its `quoted_text` is always derived server-side by slicing the
page's own stored `extracted_text` at the given offsets — never trusted
from client input — so a highlight can never claim to quote something the
source page doesn't actually contain at that position. This is a
deliberate integrity choice, not just a convenience: the alternative
(accepting a client-submitted quoted_text) would let a client-side bug,
or a tampered request, record a citation whose text doesn't match its
own document/page/offsets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.db.models import Annotation, AnnotationType, Citation, Document, DocumentPage


class InvalidHighlightRangeError(ValueError):
    """Raised when a highlight's offsets don't fall within its page's text."""


def _get_annotation_type(db: Session, name: str) -> AnnotationType:
    return db.scalars(select(AnnotationType).where(AnnotationType.name == name)).one()


def create_highlight(
    db: Session,
    document: Document,
    page: DocumentPage,
    start_offset: int,
    end_offset: int,
    actor: str,
    color: str | None = None,
) -> Annotation:
    """Create a highlight: a `citations` row (exact span) plus a linked `annotations` row.

    Raises :class:`InvalidHighlightRangeError` for an empty or
    out-of-bounds span rather than silently clamping it.
    """
    text = page.extracted_text or ""
    if not (0 <= start_offset < end_offset <= len(text)):
        raise InvalidHighlightRangeError(
            f"Highlight range [{start_offset}, {end_offset}) is invalid for a "
            f"page with {len(text)} characters of extracted text."
        )
    quoted_text = text[start_offset:end_offset]

    citation = Citation(
        document_id=document.document_id,
        page_id=page.page_id,
        start_offset=start_offset,
        end_offset=end_offset,
        quoted_text=quoted_text,
    )
    db.add(citation)
    db.flush()  # assigns citation.citation_id

    highlight_type = _get_annotation_type(db, "highlight")
    annotation = Annotation(
        case_id=document.case_id,
        document_id=document.document_id,
        page_id=page.page_id,
        citation_id=citation.citation_id,
        annotation_type_id=highlight_type.type_id,
        color=color,
        created_by=actor,
    )
    db.add(annotation)
    db.flush()

    write_custody_event(
        db, document, event_type="annotated", actor=actor,
        details={
            "annotation_type": "highlight",
            "citation_id": citation.citation_id,
            "page_number": page.page_number,
        },
    )
    return annotation


def create_note(
    db: Session, document: Document, page: DocumentPage, body_text: str, actor: str
) -> Annotation:
    """Create a page-scoped note. Raises ValueError for empty body text."""
    stripped = body_text.strip()
    if not stripped:
        raise ValueError("Note text cannot be empty.")

    note_type = _get_annotation_type(db, "note")
    annotation = Annotation(
        case_id=document.case_id,
        document_id=document.document_id,
        page_id=page.page_id,
        annotation_type_id=note_type.type_id,
        body_text=stripped,
        created_by=actor,
    )
    db.add(annotation)
    db.flush()

    write_custody_event(
        db, document, event_type="annotated", actor=actor,
        details={"annotation_type": "note", "page_number": page.page_number},
    )
    return annotation


def create_bookmark(
    db: Session,
    document: Document,
    page: DocumentPage,
    actor: str,
    body_text: str | None = None,
) -> Annotation:
    """Create a page-level bookmark. Unlike a note, body text is optional."""
    bookmark_type = _get_annotation_type(db, "bookmark")
    annotation = Annotation(
        case_id=document.case_id,
        document_id=document.document_id,
        page_id=page.page_id,
        annotation_type_id=bookmark_type.type_id,
        body_text=(body_text.strip() or None) if body_text else None,
        created_by=actor,
    )
    db.add(annotation)
    db.flush()

    write_custody_event(
        db, document, event_type="annotated", actor=actor,
        details={"annotation_type": "bookmark", "page_number": page.page_number},
    )
    return annotation


def remove_annotation(db: Session, annotation: Annotation, actor: str) -> None:
    """Soft-delete an annotation (sets `deleted_at`).

    A no-op if already removed. Never touches the annotation's linked
    citation (if it was a highlight) or the source document in any way —
    citations remain permanent traceability records even after the
    annotation that created them is removed; see
    docs/PHASE_2_PLAN.md §12.4.
    """
    if annotation.deleted_at is not None:
        return

    annotation.deleted_at = datetime.now(timezone.utc)
    write_custody_event(
        db, annotation.document, event_type="annotation_removed", actor=actor,
        details={
            "annotation_id": annotation.annotation_id,
            "annotation_type_id": annotation.annotation_type_id,
        },
    )


def list_page_annotations(db: Session, document_id: int, page_id: int) -> list[Annotation]:
    """All non-deleted annotations for one page, oldest first."""
    return db.scalars(
        select(Annotation)
        .where(
            Annotation.document_id == document_id,
            Annotation.page_id == page_id,
            Annotation.deleted_at.is_(None),
        )
        .order_by(Annotation.created_at)
    ).all()


def count_document_annotations(db: Session, document_id: int) -> dict[str, int]:
    """Non-deleted annotation counts for a whole document, by type name."""
    counts = {"highlight": 0, "note": 0, "bookmark": 0}
    rows = db.scalars(
        select(Annotation)
        .where(Annotation.document_id == document_id, Annotation.deleted_at.is_(None))
    ).all()
    for row in rows:
        counts[row.annotation_type.name] = counts.get(row.annotation_type.name, 0) + 1
    return counts
