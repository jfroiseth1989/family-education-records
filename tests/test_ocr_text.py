"""Tests for app/core/ocr/text.py -- effective_text() resolution.

Step 1 scope: native and raw-OCR branches only (the correction branch is
added in Step 3, once ocr_corrections exists) -- see the module docstring.
"""

from __future__ import annotations

from app.core.ocr.text import NATIVE, OCR_RAW, effective_text
from app.db.models import DocumentPage


def _page(**overrides) -> DocumentPage:
    defaults = dict(
        document_id=1,
        page_number=1,
        extracted_text=None,
        ocr_text=None,
        extraction_method="none",
        extraction_confidence=None,
        char_count=0,
        needs_ocr=False,
        source_sha256="deadbeef",
    )
    defaults.update(overrides)
    return DocumentPage(**defaults)


def test_effective_text_prefers_raw_ocr_over_native_when_both_present():
    """Shouldn't happen in practice (native/OCR are mutually exclusive per
    page by construction -- see docs/PHASE_3_DECISIONS.md §3), but the
    documented resolution order (ocr_text before extracted_text) is
    pinned here regardless, since it's the specified contract.
    """
    page = _page(extracted_text="native text", ocr_text="ocr text", extraction_confidence=77.0)

    result = effective_text(page)

    assert result.text == "ocr text"
    assert result.source == OCR_RAW
    assert result.confidence == 77.0


def test_effective_text_falls_back_to_native_when_no_ocr_text():
    page = _page(extracted_text="some native page text")

    result = effective_text(page)

    assert result.text == "some native page text"
    assert result.source == NATIVE
    assert result.confidence is None


def test_effective_text_returns_none_when_page_has_no_text_at_all():
    page = _page()

    result = effective_text(page)

    assert result.text is None
    assert result.source == NATIVE


def test_effective_text_treats_empty_string_as_absent():
    """An empty-string extracted_text (vs. NULL) shouldn't be treated as
    real content either -- falls through the same as None would.
    """
    page = _page(extracted_text="", ocr_text="")

    result = effective_text(page)

    assert result.text is None
