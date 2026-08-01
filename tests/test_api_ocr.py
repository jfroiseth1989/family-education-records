"""End-to-end tests for OCR job enqueueing and the jobs status page, via
the FastAPI TestClient.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 0. No OCR execution route
exists yet -- the `app` fixture disables the background worker
(tests/conftest.py), so these tests process jobs directly via
app/jobs/worker.py::process_next_job for determinism.
"""

from __future__ import annotations

from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.ocr.engine import OcrResult
from app.db.models import Document, DocumentCustodyEvent, OcrJob
from app.jobs.worker import process_next_job


def _create_case(client: TestClient, label: str = "OCR API Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_image(client: TestClient, case_id: int, filename: str = "scan.jpg"):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, b"\xff\xd8\xff\xe0fake jpeg", "image/jpeg")},
        follow_redirects=False,
    )


def _upload_text(client: TestClient, case_id: int, filename: str = "notes.txt"):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, b"Some genuinely extractable native text content.", "text/plain")},
        follow_redirects=False,
    )


# --- enqueue-on-upload -------------------------------------------------


def test_uploading_an_image_enqueues_an_ocr_job(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = _upload_image(client, case_id)
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        assert document.needs_ocr is True
        assert document.ocr_status == "queued"
        jobs = db.query(OcrJob).filter_by(document_id=document_id).all()
        assert len(jobs) == 1
        assert jobs[0].status == "queued"


def test_uploading_native_text_does_not_enqueue_an_ocr_job(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = _upload_text(client, case_id)
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        assert document.needs_ocr is False
        assert document.ocr_status is None
        jobs = db.query(OcrJob).filter_by(document_id=document_id).all()
        assert jobs == []


def test_enqueueing_writes_ocr_queued_custody_event(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = _upload_image(client, case_id)
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        events = db.query(DocumentCustodyEvent).filter_by(
            document_id=document_id, event_type="ocr_queued"
        ).all()
    assert len(events) == 1


def test_new_version_upload_also_enqueues_ocr_job(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    v1 = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("v1.txt", b"version one native text", "text/plain")},
        follow_redirects=False,
    )
    v1_id = v1.headers["location"].rsplit("/", 1)[-1]

    v2 = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("v2.jpg", b"\xff\xd8\xff\xe0fake jpeg", "image/jpeg")},
        data={"version_note": "replaced with a scan"},
        follow_redirects=False,
    )
    v2_id = int(v2.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        jobs = db.query(OcrJob).filter_by(document_id=v2_id).all()
        assert len(jobs) == 1


# --- jobs status page ----------------------------------------------------


def test_ocr_jobs_page_shows_queued_job(client: TestClient):
    case_id = _create_case(client)
    _upload_image(client, case_id, "scan.jpg")

    response = client.get(f"/cases/{case_id}/ocr-jobs")
    assert response.status_code == 200
    assert "scan.jpg" in response.text
    assert "Queued" in response.text


def test_ocr_jobs_page_shows_completed_job_after_processing(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_image(client, case_id, "scan.jpg")

    fake_result = OcrResult(text="mocked", confidence=90.0, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake_result):
        with app.state.session_factory() as db:
            process_next_job(db, app.state.vault)

    response = client.get(f"/cases/{case_id}/ocr-jobs")
    assert "Completed" in response.text


def test_ocr_jobs_page_empty_state(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/ocr-jobs")
    assert "No OCR jobs for this case yet." in response.text


def test_ocr_jobs_page_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/ocr-jobs")
    assert response.status_code == 404


def test_ocr_jobs_page_scoped_to_case(client: TestClient):
    case_a = _create_case(client, "Case A")
    case_b = _create_case(client, "Case B")
    _upload_image(client, case_a, "a.jpg")
    _upload_image(client, case_b, "b.jpg")

    response = client.get(f"/cases/{case_a}/ocr-jobs")
    assert "a.jpg" in response.text
    assert "b.jpg" not in response.text


def test_ocr_jobs_link_appears_on_case_detail_page(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert f"/cases/{case_id}/ocr-jobs" in response.text


# --- background worker disabled during tests ------------------------


def test_background_worker_is_disabled_in_test_app(app: FastAPI):
    """Pins the test-safety design from tests/conftest.py's `settings`
    fixture -- if this ever silently flips back to True, every test using
    the `app`/`client` fixtures would start spinning up a real background
    thread per test.
    """
    assert not hasattr(app.state, "ocr_worker_thread")
