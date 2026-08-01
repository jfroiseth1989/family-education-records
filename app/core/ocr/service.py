"""OCR orchestration: turning a claimed `ocr_jobs` row into `document_pages`
writes.

`run_ocr_job()` is the OCR analog of
`app/core/extraction/service.py::extract_document()`. It processes every
page flagged `needs_ocr` independently (a failure on one page never
discards another page's already-written result, or aborts the rest of
the document — docs/PHASE_3_IMPLEMENTATION_PLAN.md §6) and only ever
opens a document's stored original in read mode -- via PyMuPDF page
rendering, or directly for a standalone image file. A document's file and
hash are exactly as they were before OCR ran; verified by
tests/test_ocr_service.py, mirroring extraction's own most important
regression test.

Deliberately returns an `OcrRunResult` rather than finishing the job or
writing a custody event itself -- that's `app/jobs/worker.py`'s job (see
its module docstring), which keeps this module import-free of
`app/jobs/worker.py` (which itself imports `run_ocr_job` from here) and
keeps "did OCR run correctly" (this module) separate from "how is a
job's lifecycle recorded" (the worker).
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.extraction.dispatcher import is_image_extension
from app.core.files import compute_sha256
from app.core.ocr.engine import engine_label, is_tesseract_available, run_ocr_on_image
from app.core.vault import VaultLayout
from app.db.models import Document, DocumentPage, OcrJob, OcrTextHistory


@dataclass(frozen=True)
class OcrRunResult:
    status: str  # completed / completed_with_errors / failed
    error: str | None
    pages_ocred: list[int] = field(default_factory=list)
    pages_failed: list[int] = field(default_factory=list)


def run_ocr_job(db: Session, vault: VaultLayout, job: OcrJob) -> OcrRunResult:
    """Run OCR for every `needs_ocr` page of `job`'s document.

    Does not commit and does not touch `ocr_jobs`/custody events -- the
    caller (app/jobs/worker.py::process_next_job) does both, exactly
    once, from this result, so a claimed-but-interrupted run never leaves
    a partially-written page committed.
    """
    document = job.document
    full_path = vault.root / document.stored_path

    if not full_path.exists():
        return OcrRunResult(status="failed", error="Stored original file is missing from the vault.")

    recomputed_hash = compute_sha256(full_path)
    if recomputed_hash != document.sha256_hash:
        return OcrRunResult(
            status="failed",
            error="Stored original's hash no longer matches the recorded hash — "
            "OCR was not run against possibly-altered content.",
        )

    if not is_tesseract_available():
        return OcrRunResult(
            status="failed", error="Tesseract binary not found — see README for install instructions."
        )

    job.engine = engine_label()

    pages_to_ocr = [page for page in document.pages if page.needs_ocr]

    succeeded: list[int] = []
    failed: list[tuple[int, str]] = []
    for page in pages_to_ocr:
        try:
            _ocr_one_page(db, full_path, document, page, job)
            succeeded.append(page.page_number)
        except Exception as exc:  # noqa: BLE001 -- one bad page must not abort the job
            failed.append((page.page_number, f"{type(exc).__name__}: {exc}"[:300]))

    if failed and not succeeded:
        status = "failed"
    elif failed:
        status = "completed_with_errors"
    else:
        status = "completed"

    error = "; ".join(f"page {n}: {msg}" for n, msg in failed)[:2000] if failed else None
    return OcrRunResult(
        status=status, error=error, pages_ocred=succeeded, pages_failed=[n for n, _ in failed]
    )


def _ocr_one_page(
    db: Session, full_path: Path, document: Document, page: DocumentPage, job: OcrJob
) -> None:
    """OCR one page and write its result, archiving any prior raw text first.

    Raises on failure -- the caller catches this per-page (see
    run_ocr_job). Never touches `page.extracted_text` (native text is a
    separate, OCR-untouched column — docs/DATA_MODEL.md design principle
    #2) and never overwrites `page.ocr_text` in place without archiving
    the value it's replacing first (docs/PHASE_3_DECISIONS.md §9.3).
    """
    if is_image_extension(document.original_filename):
        result = run_ocr_on_image(full_path)
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_path = _render_pdf_page_to_image(full_path, page.page_number, Path(tmp_dir))
            result = run_ocr_on_image(rendered_path)

    if page.ocr_text is not None:
        db.add(
            OcrTextHistory(
                page_id=page.page_id,
                ocr_text=page.ocr_text,
                extraction_confidence=page.extraction_confidence,
                superseded_by_job_id=job.job_id,
            )
        )

    page.ocr_text = result.text
    page.extraction_confidence = result.confidence
    page.ocr_word_boxes = [asdict(box) for box in result.word_boxes]
    page.extraction_method = "ocr"


def _render_pdf_page_to_image(pdf_path: Path, page_number: int, tmp_dir: Path) -> Path:
    """Render one 1-indexed PDF page to a temp PNG via PyMuPDF (read-only)."""
    import fitz  # local import: keeps the PDF dependency's use scoped to this one path

    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        pixmap = page.get_pixmap()
        image_path = tmp_dir / f"page-{page_number}.png"
        pixmap.save(str(image_path))
    return image_path


def render_pdf_page_to_png_bytes(pdf_path: Path, page_number: int) -> bytes:
    """Render one 1-indexed PDF page to PNG bytes, for the OCR review UI.

    Separate from `_render_pdf_page_to_image` above (which writes to a
    temp file for pytesseract's file-path-based API): this one returns
    bytes directly, since an HTTP response has no use for a file that
    outlives the request, and a `TemporaryDirectory` would close before
    `FileResponse` could stream from it. Opens the stored original
    read-only, same as every other OCR/extraction path.
    """
    import fitz  # local import: keeps the PDF dependency's use scoped to this one path

    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        pixmap = page.get_pixmap()
        return pixmap.tobytes("png")
