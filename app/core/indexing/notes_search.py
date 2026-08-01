"""Search over annotation body text (notes and bookmarks).

Kept entirely separate from document text search
(app/core/indexing/search.py, over `document_text_fts`) so a result can
never be ambiguous between "found in the source document" and "found in
your own notes about it" -- docs/PHASE_2_PLAN.md §6/§7/§12.4.

`annotation_notes_fts` is synced explicitly here, not via triggers
(approved decision 1, §11.1): annotations are written from more UI actions
than `document_pages` ever is, so an explicit call at each write site is
more auditable than a wide trigger surface. The call sites are
`create_note`/`create_bookmark`/`remove_annotation` in
app/core/annotations/service.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.indexing.search import _quote_as_phrase
from app.db.models import Annotation


def index_annotation_note(db: Session, annotation: Annotation) -> None:
    """Add a just-created annotation's body text to the notes search index.

    A no-op when there's no body text to index (a highlight; a bookmark
    left blank) -- there's nothing to search.
    """
    if not annotation.body_text:
        return
    db.execute(
        text("INSERT INTO annotation_notes_fts(rowid, body_text) VALUES (:annotation_id, :body_text)"),
        {"annotation_id": annotation.annotation_id, "body_text": annotation.body_text},
    )


def deindex_annotation_note(db: Session, annotation: Annotation) -> None:
    """Remove a soft-deleted annotation's row from the notes search index.

    A no-op if it was never indexed (a highlight; a blank bookmark). Uses
    FTS5's external-content 'delete' command, which needs the same
    column values that were originally indexed to correctly remove the
    index's terms -- safe here because soft-delete only sets
    `deleted_at` and never touches `body_text`, so `annotation.body_text`
    at removal time is still exactly what `index_annotation_note` indexed.
    """
    if not annotation.body_text:
        return
    db.execute(
        text(
            "INSERT INTO annotation_notes_fts(annotation_notes_fts, rowid, body_text) "
            "VALUES ('delete', :annotation_id, :body_text)"
        ),
        {"annotation_id": annotation.annotation_id, "body_text": annotation.body_text},
    )


@dataclass(frozen=True)
class NoteSearchResult:
    annotation_id: int
    document_id: int
    page_id: int | None
    annotation_type: str
    snippet: str


def search_case_annotation_notes(
    db: Session, case_id: int, query: str, *, limit: int = 50
) -> list[NoteSearchResult]:
    """Search non-deleted annotation body text within one case.

    ``query`` must be non-empty -- same "query tool, not a browse-all
    view" rule as document search (see search_case_documents).
    """
    stripped_query = query.strip()
    if not stripped_query:
        return []

    sql = text(
        """
        SELECT
            a.annotation_id AS annotation_id,
            a.document_id AS document_id,
            a.page_id AS page_id,
            ant.name AS annotation_type,
            snippet(annotation_notes_fts, -1, '[', ']', '…', 10) AS snippet
        FROM annotation_notes_fts
        JOIN annotations a ON a.annotation_id = annotation_notes_fts.rowid
        JOIN annotation_types ant ON ant.type_id = a.annotation_type_id
        WHERE annotation_notes_fts MATCH :match_query
          AND a.case_id = :case_id
          AND a.deleted_at IS NULL
        ORDER BY rank
        LIMIT :limit
        """
    )
    rows = db.execute(
        sql,
        {
            "match_query": _quote_as_phrase(stripped_query),
            "case_id": case_id,
            "limit": limit,
        },
    ).mappings().all()
    return [
        NoteSearchResult(
            annotation_id=row["annotation_id"],
            document_id=row["document_id"],
            page_id=row["page_id"],
            annotation_type=row["annotation_type"],
            snippet=row["snippet"],
        )
        for row in rows
    ]
