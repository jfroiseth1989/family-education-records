"""End-to-end tests for the document viewer and annotation routes, via the
FastAPI TestClient.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.models import Annotation, Citation, Document, DocumentCustodyEvent, DocumentPage


def _create_case(client: TestClient, label: str = "Annotations API Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_text(
    client: TestClient,
    case_id: int,
    filename: str = "doc.txt",
    content: bytes = b"Hello evidence world, page one text.",
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


# --- viewer route --------------------------------------------------------


def test_view_document_renders_page_text(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Some viewable extracted text.")

    response = client.get(f"/documents/{document_id}/view?page=1")
    assert response.status_code == 200
    assert "Some viewable extracted text." in response.text


def test_view_document_nonexistent_document_returns_404(client: TestClient):
    response = client.get("/documents/99999/view")
    assert response.status_code == 404


def test_view_document_nonexistent_page_shows_message_not_error(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)

    response = client.get(f"/documents/{document_id}/view?page=999")
    assert response.status_code == 200
    assert "does not exist" in response.text


def test_view_document_single_page_shows_no_next_link(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)

    response = client.get(f"/documents/{document_id}/view?page=1")
    assert response.status_code == 200
    assert "Next page" not in response.text
    assert "Previous page" not in response.text


# --- highlight route -------------------------------------------------


def test_add_highlight_creates_citation_and_annotation(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Hello evidence world.")
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 5, "color": "yellow"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}/view?page=1"

    with app.state.session_factory() as db:
        citations = db.query(Citation).filter_by(document_id=document_id).all()
        assert len(citations) == 1
        assert citations[0].quoted_text == "Hello"

        annotations = db.query(Annotation).filter_by(document_id=document_id).all()
        assert len(annotations) == 1
        assert annotations[0].annotation_type.name == "highlight"
        assert annotations[0].citation_id == citations[0].citation_id
        assert annotations[0].color == "yellow"


def test_add_highlight_invalid_range_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Short.")
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 9999},
    )
    assert response.status_code == 400

    with app.state.session_factory() as db:
        assert db.query(Citation).filter_by(document_id=document_id).count() == 0


def test_add_highlight_page_from_other_document_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    doc1 = _upload_text(client, case_id, "a.txt", b"Doc one content.")
    doc2 = _upload_text(client, case_id, "b.txt", b"Doc two content.")
    page_id_of_doc2 = _get_page_id(app, doc2)

    response = client.post(
        f"/documents/{doc1}/annotations/highlight",
        data={"page_id": page_id_of_doc2, "start_offset": 0, "end_offset": 3},
    )
    assert response.status_code == 400


def test_add_highlight_writes_annotated_custody_event(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Custody event content.")
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 7},
    )

    with app.state.session_factory() as db:
        events = db.query(DocumentCustodyEvent).filter_by(
            document_id=document_id, event_type="annotated"
        ).all()
    assert len(events) == 1


def test_viewer_shows_created_highlight(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Visible highlight text.")
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 7},
    )

    response = client.get(f"/documents/{document_id}/view?page=1")
    assert "Visible" in response.text


# --- note route ------------------------------------------------------


def test_add_note_creates_annotation(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "This is a note."},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        annotations = db.query(Annotation).filter_by(document_id=document_id).all()
        assert len(annotations) == 1
        assert annotations[0].annotation_type.name == "note"
        assert annotations[0].body_text == "This is a note."


def test_add_note_empty_text_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "   "},
    )
    assert response.status_code == 400


def test_viewer_shows_created_note(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "A viewer-visible note."},
    )

    response = client.get(f"/documents/{document_id}/view?page=1")
    assert "A viewer-visible note." in response.text


# --- bookmark route ----------------------------------------------------


def test_add_bookmark_creates_annotation(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/bookmark",
        data={"page_id": page_id, "body_text": "Key page"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        annotations = db.query(Annotation).filter_by(document_id=document_id).all()
        assert len(annotations) == 1
        assert annotations[0].annotation_type.name == "bookmark"
        assert annotations[0].body_text == "Key page"


def test_add_bookmark_without_body_text(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    response = client.post(
        f"/documents/{document_id}/annotations/bookmark",
        data={"page_id": page_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        annotation = db.query(Annotation).filter_by(document_id=document_id).one()
        assert annotation.body_text is None


# --- remove route ------------------------------------------------------


def test_remove_annotation_soft_deletes_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Removable note."},
    )
    with app.state.session_factory() as db:
        annotation_id = db.query(Annotation).filter_by(document_id=document_id).one().annotation_id

    response = client.post(
        f"/documents/{document_id}/annotations/{annotation_id}/remove?page=1",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}/view?page=1"

    with app.state.session_factory() as db:
        annotation = db.get(Annotation, annotation_id)
        assert annotation.deleted_at is not None

    viewer_response = client.get(f"/documents/{document_id}/view?page=1")
    assert "Removable note." not in viewer_response.text


def test_remove_nonexistent_annotation_returns_404(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)

    response = client.post(f"/documents/{document_id}/annotations/99999/remove")
    assert response.status_code == 404


def test_remove_annotation_wrong_document_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    doc1 = _upload_text(client, case_id, "a.txt", b"Doc one.")
    doc2 = _upload_text(client, case_id, "b.txt", b"Doc two.")
    page_id_of_doc2 = _get_page_id(app, doc2)

    client.post(
        f"/documents/{doc2}/annotations/note",
        data={"page_id": page_id_of_doc2, "body_text": "Belongs to doc2."},
    )
    with app.state.session_factory() as db:
        annotation_id = db.query(Annotation).filter_by(document_id=doc2).one().annotation_id

    response = client.post(f"/documents/{doc1}/annotations/{annotation_id}/remove")
    assert response.status_code == 404


# --- document detail annotation summary panel --------------------------


def test_document_detail_shows_annotation_counts(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id, content=b"Summary panel text.")
    page_id = _get_page_id(app, document_id)

    client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 7},
    )
    client.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Note."},
    )

    response = client.get(f"/documents/{document_id}")
    assert "1 highlight" in response.text
    assert "1 note" in response.text
    assert "0 bookmark" in response.text
    assert "Open viewer" in response.text


def test_document_detail_shows_zero_counts_with_no_annotations(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_text(client, case_id)

    response = client.get(f"/documents/{document_id}")
    assert "0 highlight" in response.text
    assert "0 note" in response.text
    assert "0 bookmark" in response.text
