"""Plain text and RTF extraction.

Both are treated as a single page and never need OCR — they're native
text formats by definition. RTF uses `striprtf` to strip RTF control
sequences down to plain text. Both functions only ever read the source
file.
"""

from __future__ import annotations

from pathlib import Path

from striprtf.striprtf import rtf_to_text

from app.core.extraction.types import ExtractedPage, ExtractionResult


def extract_plain_text(file_path: Path) -> ExtractionResult:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    return _single_page_result(text)


def extract_rtf(file_path: Path) -> ExtractionResult:
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    text = rtf_to_text(raw)
    return _single_page_result(text)


def _single_page_result(text: str) -> ExtractionResult:
    page = ExtractedPage(
        page_number=1,
        text=text or None,
        extraction_method="native",
        char_count=len(text.strip()),
        needs_ocr=False,
    )
    return ExtractionResult(pages=[page])
