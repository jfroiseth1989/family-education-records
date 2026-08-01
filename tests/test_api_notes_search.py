"""End-to-end tests for the notes-search route/UI, via the FastAPI
TestClient.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.models import DocumentPage


def _create_case(client: TestClient, label: str = "Notes Search API Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_text(
    client: TestClient, case_id: int, filename: str = "doc.txt", content: bytes = b"Source text."
) -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _get_page_id(app: FastAPI, document_id: int) -> int:
    with app.state.session_factory() as db:
        page = db.query(DocumentPage).filter_by(document_id=document_id, page_number=1).one()
        return page.page_id


def test_notes_search_page_renders_without_a_query(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/notes-search")
    assert response.status_code == 200
    assert "Results" not in response.text


def test_notes_search_finds_a_note(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Ask the advocate about the timeline."},
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "advocate"})
    assert response.status_code == 200
    assert "Results (1)" in response.text
    assert "doc.txt" in response.text


def test_notes_search_finds_a_bookmark(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/bookmark",
        data={"page_id": page_id, "body_text": "Key deadline"},
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "deadline"})
    assert "Results (1)" in response.text


def test_notes_search_no_results_message(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)
    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Unrelated content."},
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "zzz_no_such_word"})
    assert "No matching notes" in response.text


def test_notes_search_never_returns_document_text(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(
        client, case_id, content=b"This unique_source_marker is only in the source document."
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "unique_source_marker"})
    assert "No matching notes" in response.text


def test_document_search_never_returns_annotation_notes(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)
    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "unique_note_marker text here"},
    )

    response = client.get(f"/cases/{case_id}/search", params={"q": "unique_note_marker"})
    assert "No matches found" in response.text


def test_notes_search_excludes_removed_annotation(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Removable searchable note."},
    )

    from app.db.models import Annotation

    with app.state.session_factory() as db:
        annotation_id = db.query(Annotation).filter_by(document_id=document_id).one().annotation_id

    client.post(f"/documents/{document_id}/annotations/{annotation_id}/remove?page=1")

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "Removable"})
    assert "No matching notes" in response.text


def test_notes_search_result_links_to_viewer_at_correct_page(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Linked note text."},
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "Linked note"})
    assert f"/documents/{document_id}/view?page=1" in response.text


def test_notes_search_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/notes-search", params={"q": "test"})
    assert response.status_code == 404


def test_notes_search_result_snippet_is_html_escaped(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={
            "page_id": page_id,
            "body_text": "Note mentions marker <script>alert(1)</script> right here.",
        },
    )

    response = client.get(f"/cases/{case_id}/notes-search", params={"q": "marker"})
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_notes_search_link_appears_on_case_detail_page(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert f"/cases/{case_id}/notes-search" in response.text


def test_notes_search_link_appears_on_document_search_page(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/search")
    assert f"/cases/{case_id}/notes-search" in response.text
