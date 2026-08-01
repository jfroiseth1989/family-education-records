"""PDF text extraction via PyMuPDF (fitz).

Reads a stored original's pages one at a time, extracting whatever native
text layer exists and flagging pages that look like they need OCR (an
embedded image but little/no extractable text) — see
app/core/extraction/needs_ocr.py. `fitz.open()` only ever reads from
disk; nothing here writes back to the source file.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from app.core.extraction.needs_ocr import page_needs_ocr
from app.core.extraction.types import ExtractedPage, ExtractionResult


def extract(file_path: Path) -> ExtractionResult:
    pages: list[ExtractedPage] = []
    with fitz.open(file_path) as pdf:
        for index, page in enumerate(pdf, start=1):
            text = page.get_text()
            has_visual_content = len(page.get_images()) > 0
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=text or None,
                    extraction_method="native",
                    char_count=len(text.strip()) if text else 0,
                    needs_ocr=page_needs_ocr(text, has_visual_content),
                )
            )
    return ExtractionResult(pages=pages)
