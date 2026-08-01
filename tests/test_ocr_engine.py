"""Tests for app/core/ocr/engine.py -- the pytesseract wrapper.

`run_ocr_on_image` is tested against a mocked `pytesseract.image_to_data`
call, not a real Tesseract binary -- this environment has neither the
Tesseract binary nor previously had pytesseract installed, matching
docs/PHASE_3_IMPLEMENTATION_PLAN.md §12's stated testing approach.
`is_tesseract_available` is tested against the *real*, genuinely-missing
binary in this environment -- a real, not simulated, negative case.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from app.core.ocr.engine import is_tesseract_available, run_ocr_on_image


def test_is_tesseract_available_is_false_in_this_environment():
    """This test environment has no Tesseract binary installed -- a real
    condition, not a mocked one. If this ever starts failing because
    Tesseract *is* installed, that's fine; the point is the function
    correctly reports whichever is actually true.
    """
    assert is_tesseract_available() is False


def _fake_tesseract_data() -> dict:
    """Shape-accurate stand-in for pytesseract.image_to_data(..., output_type=DICT).

    Includes a structural (-1 confidence, empty text) row, exactly as
    Tesseract's real output does for block/paragraph/line boundaries.
    """
    return {
        "text": ["", "Hello", "world"],
        "conf": [-1, 96.5, 88.0],
        "left": [0, 10, 60],
        "top": [0, 5, 5],
        "width": [0, 40, 45],
        "height": [0, 12, 12],
    }


def test_run_ocr_on_image_joins_detected_words(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"not a real png, never opened by the mock")

    with mock.patch("pytesseract.image_to_data", return_value=_fake_tesseract_data()):
        result = run_ocr_on_image(image_path)

    assert result.text == "Hello world"


def test_run_ocr_on_image_computes_mean_confidence_excluding_structural_rows(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"placeholder")

    with mock.patch("pytesseract.image_to_data", return_value=_fake_tesseract_data()):
        result = run_ocr_on_image(image_path)

    assert result.confidence == (96.5 + 88.0) / 2


def test_run_ocr_on_image_returns_word_boxes(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"placeholder")

    with mock.patch("pytesseract.image_to_data", return_value=_fake_tesseract_data()):
        result = run_ocr_on_image(image_path)

    assert len(result.word_boxes) == 2
    assert result.word_boxes[0].text == "Hello"
    assert result.word_boxes[0].confidence == 96.5
    assert result.word_boxes[1].text == "world"


def test_run_ocr_on_image_handles_no_detected_text(tmp_path: Path):
    image_path = tmp_path / "blank.png"
    image_path.write_bytes(b"placeholder")

    blank_data = {"text": ["", ""], "conf": [-1, -1], "left": [0, 0], "top": [0, 0], "width": [0, 0], "height": [0, 0]}
    with mock.patch("pytesseract.image_to_data", return_value=blank_data):
        result = run_ocr_on_image(image_path)

    assert result.text == ""
    assert result.confidence is None
    assert result.word_boxes == []


def test_run_ocr_on_image_ignores_whitespace_only_words():
    """Tesseract sometimes reports a whitespace-only "word" with a real
    (non -1) confidence for stray marks -- these shouldn't pollute the
    joined text or the word list.
    """
    from pathlib import Path as _Path

    data = {
        "text": ["Real", "   ", "text"],
        "conf": [90.0, 40.0, 85.0],
        "left": [0, 0, 0],
        "top": [0, 0, 0],
        "width": [0, 0, 0],
        "height": [0, 0, 0],
    }
    with mock.patch("pytesseract.image_to_data", return_value=data):
        result = run_ocr_on_image(_Path("/nonexistent/does-not-matter.png"))

    assert result.text == "Real text"
    assert len(result.word_boxes) == 2
