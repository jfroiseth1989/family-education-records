"""Tests for GET /documents/{document_id}/pages/{page_number}/image.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md Step 4. Serves a read-only
rendering of one page for the OCR review UI -- a standalone image
document is served directly from the stored original; a PDF page is
rendered to PNG bytes via PyMuPDF (never modifying the stored original).
"""

from __future__ import annotations

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _create_case(client: TestClient, label: str = "Page Image Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_image_document_page_image_serves_original_bytes(client: TestClient):
    case_id = _create_case(client)
    content = b"\xff\xd8\xff\xe0fake jpeg for page image test"
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("scan.jpg", content, "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    image_response = client.get(f"/documents/{document_id}/pages/1/image")

    assert image_response.status_code == 200
    assert image_response.content == content


def test_pdf_document_page_image_renders_png(client: TestClient):
    case_id = _create_case(client)

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Native page text for page-image test.")
    pdf_bytes = doc.tobytes()
    doc.close()

    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("native.pdf", pdf_bytes, "application/pdf")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    image_response = client.get(f"/documents/{document_id}/pages/1/image")

    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert image_response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_page_image_nonexistent_document_returns_404(client: TestClient):
    response = client.get("/documents/99999/pages/1/image")
    assert response.status_code == 404


def test_page_image_nonexistent_page_returns_404(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("scan.jpg", b"\xff\xd8\xff\xe0fake jpeg two", "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    image_response = client.get(f"/documents/{document_id}/pages/99/image")
    assert image_response.status_code == 404


def test_page_image_does_not_modify_stored_original(client: TestClient, app: FastAPI):
    """The route only ever opens the stored original read-only -- confirm
    its hash is unchanged after being served.
    """
    from app.core.files import compute_sha256
    from app.db.models import Document

    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("scan.jpg", b"\xff\xd8\xff\xe0fake jpeg three", "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        full_path = app.state.vault.root / document.stored_path
        before_hash = compute_sha256(full_path)

    client.get(f"/documents/{document_id}/pages/1/image")

    after_hash = compute_sha256(full_path)
    assert after_hash == before_hash
