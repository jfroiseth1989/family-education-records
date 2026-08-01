"""`effective_text()`: the one rule every consumer uses to decide "what is
this page's real text right now."

See docs/PHASE_3_IMPLEMENTATION_PLAN.md §3. Built incrementally, on
purpose, the same way `document_pages`/`citations` themselves were built
incrementally across Phase 2's steps: Step 1 implemented the native/
raw-OCR branches; Step 3 adds the correction branch here, the only place
it needs to be wired in. No consumer should ever read
`page.ocr_text`/`page.extracted_text`/`page.corrections` directly to
decide "what text represents this page" -- always through here.

Returns the *source* the text came from alongside the text itself so
callers that need to record provenance (`create_highlight()`,
docs/PHASE_3_DECISIONS.md §9.2) never have to separately re-derive which
branch fired -- one resolution, one place, both text and provenance
agree by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import DocumentPage

# Kept in sync with citations.text_source's vocabulary (see the Citation
# model docstring) -- one shared vocabulary for "what kind of text is
# this," used by both citations and (from Step 2 onward) search result
# provenance badges.
NATIVE = "native"
OCR_RAW = "ocr_raw"
OCR_CORRECTED = "ocr_corrected"


@dataclass(frozen=True)
class EffectiveText:
    text: str | None
    source: str  # NATIVE / OCR_RAW / OCR_CORRECTED
    confidence: float | None  # only meaningful for OCR_RAW/OCR_CORRECTED


def effective_text(page: DocumentPage) -> EffectiveText:
    """Resolve the text that best represents `page` right now.

    Order: the latest correction, if any exists, else current raw OCR
    text, else native extracted text, else nothing.
    `page.corrections` is ordered oldest-first (see the relationship on
    `DocumentPage`), so `page.corrections[-1]` is always the most recent
    one -- "the current correction" is never a maintained pointer, just
    "the last row," resolved here and nowhere else
    (docs/PHASE_3_DECISIONS.md §5/§10.2).

    A corrected page's confidence still reflects the *underlying OCR
    run's* confidence, not some new "confidence in the correction" concept
    -- a human correction doesn't have a probabilistic score of its own;
    the value tells a reader how confident the OCR was that a human then
    revised, which is useful context to keep (docs/PHASE_3_IMPLEMENTATION_
    PLAN.md §2).

    Native and raw-OCR text are mutually exclusive per page by
    construction -- a page is never both natively extracted successfully
    and OCR'd -- so at most one of those two branches can ever fire for a
    given page; a correction can exist on top of either, though in
    practice only ever on top of an OCR'd page (see
    app/core/ocr/corrections.py's validation).
    """
    if page.corrections:
        latest = page.corrections[-1]
        return EffectiveText(text=latest.corrected_text, source=OCR_CORRECTED, confidence=page.extraction_confidence)
    if page.ocr_text:
        return EffectiveText(text=page.ocr_text, source=OCR_RAW, confidence=page.extraction_confidence)
    if page.extracted_text:
        return EffectiveText(text=page.extracted_text, source=NATIVE, confidence=None)
    return EffectiveText(text=None, source=NATIVE, confidence=None)
