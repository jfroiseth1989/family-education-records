"""Tagging: attaching case-scoped labels to documents.

See docs/DATA_MODEL.md "tags"/"document_tags" and docs/PHASE_2_PLAN.md
§13 Step 3. Tags are scoped per case, found-or-created case-insensitively
so "IEP" and "iep" don't become two different tags, and every
attach/detach is logged as a document custody event — same discipline as
every other action taken on a document.

No rename or delete UI in Step 3: a tag persists even once nothing uses
it. Cleanup, if ever needed, is a later, deliberate feature, not an
incidental one here.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.db.models import Case, Document, DocumentTag, Tag


def find_or_create_tag(db: Session, case: Case, name: str, category: str | None = None) -> Tag:
    """Return an existing tag with this name in `case` (case-insensitive), or create one.

    Raises ValueError for an empty/whitespace-only name rather than
    silently creating a blank tag.
    """
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Tag name cannot be empty.")

    existing = db.scalars(
        select(Tag).where(
            Tag.case_id == case.case_id,
            func.lower(Tag.name) == normalized_name.lower(),
        )
    ).first()
    if existing is not None:
        return existing

    normalized_category = category.strip() if category and category.strip() else None
    tag = Tag(case_id=case.case_id, name=normalized_name, category=normalized_category)
    db.add(tag)
    db.flush()  # assigns tag.tag_id
    return tag


def tag_document(
    db: Session, document: Document, name: str, category: str | None = None, *, actor: str
) -> Tag:
    """Attach a tag (found-or-created by name within the document's case) to `document`.

    A no-op (not an error) if the document already carries this tag.
    """
    tag = find_or_create_tag(db, document.case, name, category)

    already_linked = db.get(DocumentTag, (document.document_id, tag.tag_id)) is not None
    if not already_linked:
        db.add(DocumentTag(document_id=document.document_id, tag_id=tag.tag_id))
        write_custody_event(
            db, document, event_type="tagged", actor=actor,
            details={"tag_id": tag.tag_id, "tag_name": tag.name},
        )
    return tag


def untag_document(db: Session, document: Document, tag_id: int, *, actor: str) -> None:
    """Detach a tag from `document`. A no-op if the document doesn't carry it.

    Never deletes the `Tag` row itself, even if this was its last document
    — see module docstring.
    """
    link = db.get(DocumentTag, (document.document_id, tag_id))
    if link is None:
        return

    tag_name = link.tag.name
    db.delete(link)
    write_custody_event(
        db, document, event_type="untagged", actor=actor,
        details={"tag_id": tag_id, "tag_name": tag_name},
    )


def list_case_tags(db: Session, case_id: int) -> list[Tag]:
    return db.scalars(select(Tag).where(Tag.case_id == case_id).order_by(Tag.name)).all()
