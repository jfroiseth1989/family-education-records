"""Shared data types for extraction results.

Kept separate from individual extractor modules and from the
orchestration/database layer (service.py) so every extractor depends only
on this small, stable contract — not on each other, on SQLAlchemy, or on
how results end up persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractedPage:
    """One page's worth of extraction output, before it's written to the DB."""

    page_number: int
    text: str | None
    extraction_method: str  # "native" | "none" -- OCR ("ocr") is Phase 3
    char_count: int
    needs_ocr: bool


@dataclass(frozen=True)
class ExtractedAttachment:
    """A file embedded in another document (currently: an email attachment).

    Carries raw bytes rather than a path — the orchestration layer decides
    where (and whether) to write them to disk via the normal ingestion path,
    keeping extractor modules free of filesystem/vault concerns.
    """

    filename: str
    content: bytes
    mime_type: str | None


@dataclass(frozen=True)
class ExtractionResult:
    """The full output of extracting one document."""

    pages: list[ExtractedPage]
    attachments: list[ExtractedAttachment] = field(default_factory=list)
