"""The Tesseract wrapper: one page image in, raw text + confidence + word
boxes out.

Runs Tesseract fully offline via `pytesseract` -- no cloud vision API of
any kind, ever (docs/PRIVACY_SECURITY.md §2, locked). This module is the
one place `pytesseract` is imported anywhere in the application, so the
OCR engine call is a single, easily-mocked seam for tests (see
tests/test_ocr_engine.py) -- the real Tesseract binary isn't installed in
every environment this test suite runs in, so the default suite mocks at
this boundary; a separate, explicitly-marked integration test exercises
the real binary when available.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytesseract

# Fixed single language for v1 -- see docs/PHASE_3_DECISIONS.md §5. A
# configurable/multi-language setting is a real, concrete extension to
# make later if a real need for it arises; not built ahead of one.
OCR_LANGUAGE = "eng"

# Tesseract's own sentinel for "not a real word" rows (block/paragraph/
# line boundaries) in image_to_data's per-row confidence column.
_NON_WORD_CONFIDENCE = -1


@dataclass(frozen=True)
class WordBox:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float | None  # mean of per-word confidences; None if no words detected
    word_boxes: list[WordBox] = field(default_factory=list)


def is_tesseract_available() -> bool:
    """Cheap, no-OCR-run check for whether the Tesseract binary is usable.

    Checked once per job (not per page) at the start of run_ocr_job --
    see docs/PHASE_3_IMPLEMENTATION_PLAN.md §6.
    """
    if shutil.which("tesseract") is None:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


def engine_label() -> str:
    """Return "tesseract-<version>", captured at run time and stored on
    ocr_jobs.engine so a later Tesseract upgrade doesn't retroactively
    obscure what actually produced older OCR text
    (docs/PHASE_3_IMPLEMENTATION_PLAN.md §2). Only meaningful to call
    after is_tesseract_available() has returned True.
    """
    return f"tesseract-{pytesseract.get_tesseract_version()}"


def run_ocr_on_image(image_path: Path) -> OcrResult:
    """Run Tesseract on a single page image and return text + confidence + boxes.

    Raises whatever pytesseract/Tesseract itself raises on failure (e.g.
    a corrupt or unreadable image) -- the caller (app/core/ocr/service.py)
    is responsible for catching this per-page, so one bad page doesn't
    abort a whole document's OCR run.
    """
    data = pytesseract.image_to_data(
        str(image_path), lang=OCR_LANGUAGE, output_type=pytesseract.Output.DICT
    )

    words: list[str] = []
    boxes: list[WordBox] = []
    confidences: list[float] = []
    for i, raw_text in enumerate(data["text"]):
        text = raw_text.strip()
        if not text:
            continue
        confidence = float(data["conf"][i])
        if confidence == _NON_WORD_CONFIDENCE:
            continue
        words.append(text)
        confidences.append(confidence)
        boxes.append(
            WordBox(
                text=text,
                left=data["left"][i],
                top=data["top"][i],
                width=data["width"][i],
                height=data["height"][i],
                confidence=confidence,
            )
        )

    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    return OcrResult(text=" ".join(words), confidence=mean_confidence, word_boxes=boxes)
