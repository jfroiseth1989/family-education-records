"""DOCX text extraction via python-docx.

DOCX has no fixed pagination, so it's stored as a single "page" (page 1)
containing the full paragraph-joined text — see docs/PHASE_2_PLAN.md §5.2.
`python-docx` opens the file for parsing only; nothing here writes back to
the source file.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument

from app.core.extraction.types import ExtractedPage, ExtractionResult


def extract(file_path: Path) -> ExtractionResult:
    docx_document = DocxDocument(str(file_path))
    text = "\n".join(paragraph.text for paragraph in docx_document.paragraphs)
    page = ExtractedPage(
        page_number=1,
        text=text or None,
        extraction_method="native",
        char_count=len(text.strip()),
        # DOCX is a native text format by definition -- there's no "scanned
        # page" concept for it the way there is for PDF/image formats.
        needs_ocr=False,
    )
    return ExtractionResult(pages=[page])
