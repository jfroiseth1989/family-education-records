"""`effective_text()`: the one rule every consumer uses to decide "what is
this page's real text right now."

See docs/PHASE_3_IMPLEMENTATION_PLAN.md §3. Built incrementally, on
purpose, the same way `document_pages`/`citations` themselves were built
incrementally across Phase 2's steps: Step 1 implements the native/raw-OCR
branches (the only ones possible before `ocr_corrections` exists); Step 3
adds the correction branch on top, in this same function, once that table
exists. No consumer should ever read `page.ocr_text`/`page.extracted_text`
directly to decide "what text represents this page" -- always through
here, so a future added branch (corrections) only has to be wired into
one place.

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
# model docstring) -- "ocr_corrected" is unreachable until Step 3 adds
# ocr_corrections, but the vocabulary is declared here now so Step 3 only
# has to add a branch, not invent a new value.
NATIVE = "native"
OCR_RAW = "ocr_raw"
OCR_CORRECTED = "ocr_corrected"


@dataclass(frozen=True)
class EffectiveText:
    text: str | None
    source: str  # NATIVE / OCR_RAW / (OCR_CORRECTED from Step 3 onward)
    confidence: float | None  # only meaningful for OCR_RAW/OCR_CORRECTED


def effective_text(page: DocumentPage) -> EffectiveText:
    """Resolve the text that best represents `page` right now.

    Step 1 order: current raw OCR text, if any, else native extracted
    text, else nothing. (Step 3 will check for a correction first, ahead
    of raw OCR.) Native and OCR text are mutually exclusive per page by
    construction -- a page is never both natively extracted successfully
    and OCR'd -- so in practice at most one of these branches can ever
    fire for a given page today.
    """
    if page.ocr_text:
        return EffectiveText(text=page.ocr_text, source=OCR_RAW, confidence=page.extraction_confidence)
    if page.extracted_text:
        return EffectiveText(text=page.extracted_text, source=NATIVE, confidence=None)
    return EffectiveText(text=None, source=NATIVE, confidence=None)
