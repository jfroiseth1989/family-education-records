"""Selects the right extractor for a document by file extension.

Returns None for anything unsupported, which the orchestration layer
(service.py) turns into `extraction_status = "unsupported_format"` rather
than an error — see docs/PHASE_2_PLAN.md §5.2 and §11 decision 6. Images
have no extractor at all: they're handled directly by the orchestration
layer (flagged `needs_ocr` immediately) since there's no text layer to
attempt extraction on.

Dispatch is by filename extension rather than the browser-supplied MIME
type, which can be missing or unreliable for an upload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.core.extraction import docx as docx_extractor
from app.core.extraction import email as email_extractor
from app.core.extraction import pdf as pdf_extractor
from app.core.extraction import text as text_extractor
from app.core.extraction.types import ExtractionResult

Extractor = Callable[[Path], ExtractionResult]

_EXTENSION_DISPATCH: dict[str, Extractor] = {
    ".pdf": pdf_extractor.extract,
    ".docx": docx_extractor.extract,
    ".txt": text_extractor.extract_plain_text,
    ".rtf": text_extractor.extract_rtf,
    ".eml": email_extractor.extract,
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def get_extractor(original_filename: str) -> Extractor | None:
    """Return the extractor function for a filename, or None if unsupported."""
    suffix = Path(original_filename).suffix.lower()
    return _EXTENSION_DISPATCH.get(suffix)


def is_image_extension(original_filename: str) -> bool:
    return Path(original_filename).suffix.lower() in IMAGE_EXTENSIONS
