"""The deterministic "needs OCR" heuristic.

A page needs OCR when it doesn't have enough extractable text *and* the
source format indicates it should have visible content (e.g. it contains
an embedded image) — see docs/ARCHITECTURE.md §3.3 and
docs/PHASE_2_PLAN.md §5.3. This is deliberately not a guess based on file
type alone: a genuinely blank page (no image, no text) isn't flagged just
for being text-sparse.

The threshold is a named constant, not inlined, so it can be tuned without
touching any extractor's logic.
"""

from __future__ import annotations

MIN_EXTRACTABLE_WORDS = 10


def page_needs_ocr(text: str | None, has_visual_content: bool) -> bool:
    """Return True if this page's content looks like it needs OCR.

    ``has_visual_content`` should be True when the source page is known to
    contain an image (or otherwise looks like it has visible content worth
    extracting) — a plain page with a handful of words and no image is
    just a short page, not one needing OCR.
    """
    if not has_visual_content:
        return False
    word_count = len((text or "").split())
    return word_count < MIN_EXTRACTABLE_WORDS
