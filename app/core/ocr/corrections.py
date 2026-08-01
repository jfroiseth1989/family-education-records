"""Human corrections to a page's OCR text.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 3 and
docs/PHASE_3_DECISIONS.md §1/§5/§9.3. `document_pages.ocr_text` (raw OCR
output) is never touched here -- `docs/PRIVACY_SECURITY.md` §3 locks this
explicitly: "OCR corrections are stored as a separate ... layer, never as
an overwrite of the original OCR output." A correction only ever adds a
new, append-only `ocr_corrections` row and updates what
`effective_text()` (app/core/ocr/text.py) will resolve to for this page
going forward -- it never changes anything about a citation already made
before it (docs/PHASE_3_DECISIONS.md §9.2/§10.3).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.custody import write_custody_event
from app.core.indexing.search import reindex_page_ocr_text
from app.core.ocr.text import effective_text
from app.db.models import DocumentPage, OcrCorrection


def create_ocr_correction(
    db: Session, page: DocumentPage, corrected_text: str, actor: str
) -> OcrCorrection:
    """Record a human correction to `page`'s OCR text.

    Raises ``ValueError`` if the page has no OCR text to correct yet (a
    correction only makes sense for a page that's actually been OCR'd),
    or if the corrected text is empty. Never modifies `page.ocr_text`.
    Does not commit -- the caller controls the transaction boundary, same
    convention as every other core module in this application.
    """
    if page.ocr_text is None:
        raise ValueError("This page has no OCR text to correct yet.")

    stripped = corrected_text.strip()
    if not stripped:
        raise ValueError("Corrected text cannot be empty.")

    # What's *currently* indexed for this page, before this correction is
    # added -- needed to correctly reindex below (see
    # reindex_page_ocr_text's docstring for why this isn't always simply
    # page.ocr_text once a prior correction already exists).
    previous = effective_text(page)

    correction = OcrCorrection(page_id=page.page_id, corrected_text=stripped, corrected_by=actor)
    db.add(correction)
    db.flush()  # assigns correction.correction_id
    # Keep the in-memory relationship consistent immediately, rather than
    # relying on a later expire/refresh -- a second correction created
    # later in the same session must see this one via effective_text().
    page.corrections.append(correction)

    reindex_page_ocr_text(
        db,
        page.page_id,
        extracted_text=page.extracted_text,
        previous_ocr_value=previous.text,
        new_ocr_value=stripped,
    )

    write_custody_event(
        db,
        page.document,
        event_type="ocr_corrected",
        actor=actor,
        details={
            "page_id": page.page_id,
            "page_number": page.page_number,
            "correction_id": correction.correction_id,
        },
    )
    return correction
