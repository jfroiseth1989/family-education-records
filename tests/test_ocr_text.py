"""Tests for app/core/ocr/text.py -- effective_text() resolution.

Native, raw-OCR, and (Phase 3 Step 3) correction branches -- see the
module docstring.
"""

from __future__ import annotations

from app.core.ocr.text import NATIVE, OCR_CORRECTED, OCR_RAW, effective_text
from app.db.models import DocumentPage, OcrCorrection


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


# --- correction branch (Phase 3 Step 3) -----------------------------------


def test_effective_text_prefers_correction_over_raw_ocr():
    page = _page(ocr_text="raw ocr guess", extraction_confidence=60.0)
    page.corrections.append(OcrCorrection(page_id=1, corrected_text="human-corrected text", corrected_by="tester"))

    result = effective_text(page)

    assert result.text == "human-corrected text"
    assert result.source == OCR_CORRECTED


def test_effective_text_correction_confidence_reflects_underlying_ocr_run():
    """A correction has no confidence score of its own -- the value
    carried forward is the underlying OCR run's confidence, useful
    context about what a human was correcting, not a new concept.
    """
    page = _page(ocr_text="raw ocr guess", extraction_confidence=42.0)
    page.corrections.append(OcrCorrection(page_id=1, corrected_text="corrected", corrected_by="tester"))

    result = effective_text(page)

    assert result.confidence == 42.0


def test_effective_text_uses_the_latest_of_multiple_corrections():
    page = _page(ocr_text="raw ocr guess")
    page.corrections.append(OcrCorrection(page_id=1, corrected_text="first correction", corrected_by="c1"))
    page.corrections.append(OcrCorrection(page_id=1, corrected_text="second correction", corrected_by="c2"))

    result = effective_text(page)

    assert result.text == "second correction"
