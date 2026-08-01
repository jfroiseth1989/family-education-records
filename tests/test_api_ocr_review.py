"""Tests for the OCR review UI: GET /documents/{id}/ocr-review, the
correction POST route, and the reprocess POST route.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 4. Uses the FastAPI
TestClient end-to-end, processing OCR jobs directly via
app/jobs/worker.py::process_next_job (the `app` fixture disables the
background worker) for determinism, same pattern as
tests/test_api_ocr.py.
"""

from __future__ import annotations

from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.ocr.engine import OcrResult
from app.db.models import Document, DocumentCustodyEvent, OcrCorrection, OcrJob
from app.jobs.worker import process_next_job


def _create_case(client: TestClient, label: str = "OCR Review Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_and_ocr(
    client: TestClient, app: FastAPI, case_id: int, filename: str = "scan.jpg", text: str = "raw ocr text", confidence: float = 60.0
) -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, b"\xff\xd8\xff\xe0" + filename.encode(), "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    fake_result = OcrResult(text=text, confidence=confidence, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake_result):
        with app.state.session_factory() as db:
            process_next_job(db, app.state.vault)

    return document_id


def _upload_native_text(client: TestClient, case_id: int, filename: str = "notes.txt") -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, b"Some genuinely extractable native text content.", "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


# --- review page -----------------------------------------------------------


def test_review_page_shows_effective_text_and_confidence(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id, text="recognized text here", confidence=73.5)

    response = client.get(f"/documents/{document_id}/ocr-review")

    assert response.status_code == 200
    assert "recognized text here" in response.text
    assert "73.5" in response.text
    assert "ocr_raw" in response.text


def test_review_page_nonexistent_document_returns_404(client: TestClient):
    response = client.get("/documents/99999/ocr-review")
    assert response.status_code == 404


def test_review_page_document_with_no_ocr_pages_returns_404(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_native_text(client, case_id)

    response = client.get(f"/documents/{document_id}/ocr-review")
    assert response.status_code == 404


def test_review_page_shows_correction_history(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id, text="wrongtext here")

    client.post(
        f"/documents/{document_id}/ocr-review/pages/1/correct",
        data={"corrected_text": "correctedtext here"},
        follow_redirects=False,
    )

    response = client.get(f"/documents/{document_id}/ocr-review")
    assert "correctedtext here" in response.text
    assert "ocr_corrected" in response.text


# --- correction route --------------------------------------------------


def test_correction_route_creates_correction_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id)

    response = client.post(
        f"/documents/{document_id}/ocr-review/pages/1/correct",
        data={"corrected_text": "a human corrected version"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}/ocr-review?page=1"

    with app.state.session_factory() as db:
        corrections = db.query(OcrCorrection).all()
        assert len(corrections) == 1
        assert corrections[0].corrected_text == "a human corrected version"


def test_correction_route_rejects_empty_text(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id)

    response = client.post(
        f"/documents/{document_id}/ocr-review/pages/1/correct",
        data={"corrected_text": "   "},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_correction_route_nonexistent_page_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id)

    response = client.post(
        f"/documents/{document_id}/ocr-review/pages/99/correct",
        data={"corrected_text": "whatever"},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- reprocess route ------------------------------------------------------


def test_reprocess_route_enqueues_new_job(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_and_ocr(client, app, case_id)

    response = client.post(
        f"/documents/{document_id}/ocr-review/reprocess?page=1", follow_redirects=False
    )

    assert response.status_code == 303
    with app.state.session_factory() as db:
        jobs = db.query(OcrJob).filter_by(document_id=document_id).all()
        assert len(jobs) == 2
        events = [
            e.event_type
            for e in db.query(DocumentCustodyEvent).filter_by(document_id=document_id).all()
        ]
        assert "ocr_reprocessed" in events


def test_reprocess_route_rejects_when_job_already_active(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("scan2.jpg", b"\xff\xd8\xff\xe0still queued", "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    # Never processed -- ocr_status is still "queued" from upload-time enqueue.
    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        assert document.ocr_status == "queued"

    response = client.post(
        f"/documents/{document_id}/ocr-review/reprocess", follow_redirects=False
    )
    assert response.status_code == 400

    with app.state.session_factory() as db:
        jobs = db.query(OcrJob).filter_by(document_id=document_id).all()
        assert len(jobs) == 1


def test_reprocess_route_nonexistent_document_returns_404(client: TestClient):
    response = client.post("/documents/99999/ocr-review/reprocess", follow_redirects=False)
    assert response.status_code == 404
