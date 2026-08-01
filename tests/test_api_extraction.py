"""End-to-end tests confirming extraction runs automatically after upload
and its status is visible in the UI (docs/PHASE_2_PLAN.md §12.1).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.models import Document


def _create_case(client: TestClient, label: str = "Extraction Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_uploading_text_file_extracts_automatically(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("notes.txt", b"Some extractable plain text content.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)

    assert document.extraction_status == "completed"
    assert document.page_count == 1
    assert document.needs_ocr is False


def test_document_detail_page_shows_extraction_completed_status(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("notes.txt", b"Some content here.", "text/plain")},
        follow_redirects=False,
    )
    detail_response = client.get(response.headers["location"])

    assert "Completed" in detail_response.text
    assert "1 page" in detail_response.text


def test_case_list_shows_extracted_badge(client: TestClient):
    case_id = _create_case(client)
    client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("notes.txt", b"Some content here.", "text/plain")},
    )
    list_response = client.get(f"/cases/{case_id}")
    assert "Extracted" in list_response.text


def test_unsupported_format_shows_unsupported_badge(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("data.csv", b"a,b,c\n1,2,3\n", "text/csv")},
        follow_redirects=False,
    )
    detail_response = client.get(response.headers["location"])
    assert "Unsupported format" in detail_response.text

    list_response = client.get(f"/cases/{case_id}")
    assert "Unsupported format" in list_response.text


def test_corrupt_pdf_upload_still_succeeds_with_failed_extraction_visible(
    client: TestClient, app: FastAPI
):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("corrupt.pdf", b"not a real pdf", "application/pdf")},
        follow_redirects=False,
    )
    # Ingestion succeeds (303 redirect), never a 500, regardless of extraction outcome.
    assert response.status_code == 303

    with app.state.session_factory() as db:
        document_id = int(response.headers["location"].rsplit("/", 1)[-1])
        document = db.get(Document, document_id)
        assert document.extraction_status == "failed"
        assert document.extraction_error

    detail_response = client.get(response.headers["location"])
    assert "Failed" in detail_response.text
    assert "Error" in detail_response.text


def test_image_upload_shows_needs_ocr_badge(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("scan.jpg", b"\xff\xd8\xff\xe0fake jpeg", "image/jpeg")},
        follow_redirects=False,
    )
    detail_response = client.get(response.headers["location"])
    assert "Needs OCR" in detail_response.text

    list_response = client.get(f"/cases/{case_id}")
    assert "Needs OCR" in list_response.text


def test_new_version_upload_also_extracts(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    v1_response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("v1.txt", b"version one content", "text/plain")},
        follow_redirects=False,
    )
    v1_id = v1_response.headers["location"].rsplit("/", 1)[-1]

    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("v2.txt", b"version two, corrected content", "text/plain")},
        data={"version_note": "corrected"},
        follow_redirects=False,
    )
    v2_id = int(v2_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        v2_document = db.get(Document, v2_id)

    assert v2_document.extraction_status == "completed"
    assert v2_document.page_count == 1
