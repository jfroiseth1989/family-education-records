"""End-to-end tests for document ingestion, viewing, verification, and
version-linking routes, via the FastAPI TestClient.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Document, DocumentCustodyEvent


def _create_case(client: TestClient, label: str = "Doc Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload(client: TestClient, case_id: int, filename: str, content: bytes, **extra):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        data=extra,
        follow_redirects=False,
    )


def test_upload_document_ingests_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)

    response = _upload(client, case_id, "iep.txt", b"IEP content here", source="scanned by parent")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/documents/")

    with app.state.session_factory() as db:
        documents = db.scalars(select(Document).where(Document.case_id == case_id)).all()
    assert len(documents) == 1
    assert documents[0].original_filename == "iep.txt"
    assert documents[0].source == "scanned by parent"


def test_document_detail_page_shows_hash_and_custody_log(client: TestClient):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "iep.txt", b"IEP content here")
    detail_url = upload_response.headers["location"]

    response = client.get(detail_url)
    assert response.status_code == 200
    assert "iep.txt" in response.text
    assert "imported" in response.text


def test_download_document_file_returns_original_bytes(client: TestClient):
    case_id = _create_case(client)
    content = b"exact original bytes"
    upload_response = _upload(client, case_id, "iep.txt", content)
    document_id = upload_response.headers["location"].rsplit("/", 1)[-1]

    response = client.get(f"/documents/{document_id}/file")
    assert response.status_code == 200
    assert response.content == content


def test_uploading_exact_duplicate_is_rejected(client: TestClient):
    case_id = _create_case(client)
    content = b"same bytes both times"

    first = _upload(client, case_id, "iep.txt", content)
    assert first.status_code == 303

    second = _upload(client, case_id, "iep-renamed.txt", content)
    assert second.status_code == 409


def test_verify_document_endpoint_logs_hash_verified_event(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "iep.txt", b"content")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    response = client.post(f"/documents/{document_id}/verify", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        events = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "hash_verified",
            )
        ).all()
    assert len(events) == 1
    assert events[0].details["matches"] is True


def test_upload_new_version_links_and_marks_current(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    v1_response = _upload(client, case_id, "iep-v1.txt", b"version one content")
    v1_id = int(v1_response.headers["location"].rsplit("/", 1)[-1])

    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("iep-v2.txt", b"version two, corrected", "text/plain")},
        data={"version_note": "corrected the evaluation date"},
        follow_redirects=False,
    )
    assert v2_response.status_code == 303
    v2_id = int(v2_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        doc_v1 = db.get(Document, v1_id)
        doc_v2 = db.get(Document, v2_id)

    assert doc_v1.is_current_version is False
    assert doc_v2.is_current_version is True
    assert doc_v2.supersedes_document_id == v1_id
    assert doc_v2.version_group_id == doc_v1.version_group_id

    # The original v1 file must still be intact and readable, unmodified.
    original_file_response = client.get(f"/documents/{v1_id}/file")
    assert original_file_response.content == b"version one content"


def test_document_detail_shows_version_history(client: TestClient):
    case_id = _create_case(client)
    v1_response = _upload(client, case_id, "iep-v1.txt", b"v1")
    v1_id = v1_response.headers["location"].rsplit("/", 1)[-1]

    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("iep-v2.txt", b"v2", "text/plain")},
        data={"version_note": "reissued"},
        follow_redirects=False,
    )
    v2_location = v2_response.headers["location"]

    response = client.get(v2_location)
    assert response.status_code == 200
    assert "iep-v1.txt" in response.text
    assert "iep-v2.txt" in response.text
    assert "current version" in response.text.lower()
    assert "superseded" in response.text.lower()


def test_get_nonexistent_document_returns_404(client: TestClient):
    response = client.get("/documents/99999")
    assert response.status_code == 404
