"""End-to-end tests for the search result provenance badge (Phase 3 Step
2). Kept separate from tests/test_api_search.py rather than modifying
that Phase 2 Step 2 file directly.
"""

from __future__ import annotations

from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.ocr.engine import OcrResult
from app.db.models import Document, DocumentPage


def _create_case(client: TestClient, label: str = "OCR Search Badge Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_image(client: TestClient, case_id: int, filename: str = "scan.jpg"):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, b"\xff\xd8\xff\xe0fake jpeg for badge test", "image/jpeg")},
        follow_redirects=False,
    )


def _upload_text(client: TestClient, case_id: int, filename: str, content: bytes):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        follow_redirects=False,
    )


def _run_ocr(app: FastAPI, ocr_text: str):
    from app.jobs.worker import process_next_job

    fake = OcrResult(text=ocr_text, confidence=85.0, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake):
        with app.state.session_factory() as db:
            process_next_job(db, app.state.vault)


def test_search_result_for_ocrd_page_shows_ocr_badge(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_image(client, case_id)
    _run_ocr(app, "a distinctive badge test keyword")

    response = client.get(f"/cases/{case_id}/search", params={"q": "distinctive badge test"})
    assert response.status_code == 200
    assert ">OCR<" in response.text


def test_search_result_for_native_page_shows_no_ocr_badge(client: TestClient):
    case_id = _create_case(client)
    _upload_text(client, case_id, "native.txt", b"a distinctive native badge keyword here")

    response = client.get(f"/cases/{case_id}/search", params={"q": "distinctive native badge"})
    assert response.status_code == 200
    assert "native.txt" in response.text
    assert ">OCR<" not in response.text


def test_search_results_mixing_native_and_ocr_each_show_correct_badge(
    client: TestClient, app: FastAPI
):
    case_id = _create_case(client)
    _upload_text(client, case_id, "native.txt", b"shared mixed_badge_keyword in native text")
    _upload_image(client, case_id, "scan.jpg")
    _run_ocr(app, "shared mixed_badge_keyword in ocr text")

    response = client.get(f"/cases/{case_id}/search", params={"q": "mixed_badge_keyword"})
    assert response.status_code == 200
    assert "native.txt" in response.text
    assert "scan.jpg" in response.text
    # Exactly one OCR badge -- the native result must not get one.
    assert response.text.count(">OCR<") == 1
