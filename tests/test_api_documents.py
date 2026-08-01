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


def test_upload_without_document_date_leaves_it_unknown(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "iep.txt", b"content")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)

    assert document.document_date is None
    assert document.document_date_source is None
    assert document.document_date_precision is None


def test_upload_with_document_date_records_manual_source(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(
        client, case_id, "iep.txt", b"content", document_date="2024-03-15"
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)

    assert document.document_date.strftime("%Y-%m-%d") == "2024-03-15"
    assert document.document_date_source == "manual"
    assert document.document_date_precision == "exact"


def test_upload_with_approximate_document_date(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(
        client,
        case_id,
        "iep.txt",
        b"content",
        document_date="2024-03-01",
        document_date_approximate="true",
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)

    assert document.document_date_precision == "approximate"


def test_upload_with_invalid_document_date_returns_400(client: TestClient):
    case_id = _create_case(client)
    response = _upload(client, case_id, "iep.txt", b"content", document_date="not-a-date")
    assert response.status_code == 400


def test_document_detail_page_shows_document_date(client: TestClient):
    case_id = _create_case(client)
    upload_response = _upload(
        client, case_id, "iep.txt", b"content", document_date="2024-06-01"
    )
    detail_url = upload_response.headers["location"]

    response = client.get(detail_url)
    assert "2024-06-01" in response.text


def test_document_detail_page_shows_unknown_when_no_date(client: TestClient):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "iep.txt", b"content")
    detail_url = upload_response.headers["location"]

    response = client.get(detail_url)
    assert "Unknown / not recorded" in response.text


def test_new_version_document_date_is_independent_of_prior_version(
    client: TestClient, app: FastAPI
):
    """A reissued version's date must be entered fresh, never silently
    inherited from the document it supersedes.
    """
    case_id = _create_case(client)
    v1_response = _upload(
        client, case_id, "iep-v1.txt", b"v1", document_date="2023-01-10"
    )
    v1_id = v1_response.headers["location"].rsplit("/", 1)[-1]

    # Upload a new version with no date entered -- it must NOT inherit v1's date.
    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("iep-v2.txt", b"v2", "text/plain")},
        data={"version_note": "reissued"},
        follow_redirects=False,
    )
    v2_id = int(v2_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        doc_v1 = db.get(Document, int(v1_id))
        doc_v2 = db.get(Document, v2_id)

    assert doc_v1.document_date.strftime("%Y-%m-%d") == "2023-01-10"
    assert doc_v2.document_date is None


def test_new_version_can_have_its_own_document_date(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    v1_response = _upload(client, case_id, "iep-v1.txt", b"v1", document_date="2023-01-10")
    v1_id = v1_response.headers["location"].rsplit("/", 1)[-1]

    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("iep-v2.txt", b"v2", "text/plain")},
        data={"version_note": "reissued", "document_date": "2024-02-20"},
        follow_redirects=False,
    )
    v2_id = int(v2_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        doc_v2 = db.get(Document, v2_id)

    assert doc_v2.document_date.strftime("%Y-%m-%d") == "2024-02-20"
