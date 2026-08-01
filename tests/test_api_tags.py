"""End-to-end tests for tag routes/UI, via the FastAPI TestClient."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.models import Document, DocumentCustodyEvent, Tag


def _create_case(client: TestClient, label: str = "Tag API Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload(client: TestClient, case_id: int, filename: str = "doc.txt", content: bytes = b"content"):
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_add_tag_redirects_and_persists(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)

    response = client.post(
        f"/documents/{document_id}/tags",
        data={"name": "IEP", "category": "record-type"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}"

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        tag_names = [link.tag.name for link in document.tag_links]
    assert tag_names == ["IEP"]


def test_add_tag_with_empty_name_returns_400(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)

    response = client.post(f"/documents/{document_id}/tags", data={"name": "   "})
    assert response.status_code == 400


def test_add_tag_to_nonexistent_document_returns_404(client: TestClient):
    response = client.post("/documents/99999/tags", data={"name": "IEP"})
    assert response.status_code == 404


def test_remove_tag_redirects_and_removes(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)
    client.post(f"/documents/{document_id}/tags", data={"name": "IEP"})

    with app.state.session_factory() as db:
        tag_id = db.query(Tag).filter_by(case_id=case_id, name="IEP").one().tag_id

    response = client.post(f"/documents/{document_id}/tags/{tag_id}/remove", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        assert document.tag_links == []


def test_remove_nonexistent_tag_link_is_a_noop_not_an_error(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)

    response = client.post(f"/documents/{document_id}/tags/99999/remove", follow_redirects=False)
    assert response.status_code == 303  # no-op, not a 404/400


def test_document_detail_page_shows_tags_panel(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)
    client.post(f"/documents/{document_id}/tags", data={"name": "IEP", "category": "record-type"})

    response = client.get(f"/documents/{document_id}")
    assert "IEP" in response.text
    assert "record-type" in response.text


def test_document_detail_page_shows_no_tags_message(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)

    response = client.get(f"/documents/{document_id}")
    assert "No tags yet" in response.text


def test_case_list_shows_tag_badges(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)
    client.post(f"/documents/{document_id}/tags", data={"name": "IEP"})

    response = client.get(f"/cases/{case_id}")
    assert "IEP" in response.text


def test_adding_duplicate_tag_reuses_existing_tag(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    doc1 = _upload(client, case_id, "a.txt")
    doc2 = _upload(client, case_id, "b.txt", b"other content")

    client.post(f"/documents/{doc1}/tags", data={"name": "IEP"})
    client.post(f"/documents/{doc2}/tags", data={"name": "iep"})  # different case, same tag

    with app.state.session_factory() as db:
        tags = db.query(Tag).filter_by(case_id=case_id).all()
    assert len(tags) == 1


def test_tagging_writes_custody_event(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)
    client.post(f"/documents/{document_id}/tags", data={"name": "IEP"})

    with app.state.session_factory() as db:
        events = db.query(DocumentCustodyEvent).filter_by(
            document_id=document_id, event_type="tagged"
        ).all()
    assert len(events) == 1


def test_search_filters_results_by_tag(client: TestClient):
    case_id = _create_case(client)
    doc1 = _upload(client, case_id, "tagged.txt", b"speech therapy evaluation content, copy A")
    doc2 = _upload(client, case_id, "untagged.txt", b"speech therapy evaluation content, copy B")
    client.post(f"/documents/{doc1}/tags", data={"name": "IEP"})

    response = client.get(f"/cases/{case_id}/search", params={"q": "speech therapy"})
    assert "tagged.txt" in response.text
    assert "untagged.txt" in response.text  # both present without a tag filter

    # Now filter by the tag's id.
    tag_id_response = client.get(f"/documents/{doc1}")
    # Extract tag id from the remove-form action in the HTML rather than
    # querying the DB directly, exercising the real rendered link.
    import re

    match = re.search(r"/documents/\d+/tags/(\d+)/remove", tag_id_response.text)
    assert match is not None
    tag_id = match.group(1)

    filtered = client.get(f"/cases/{case_id}/search", params={"q": "speech therapy", "tag_id": tag_id})
    assert "tagged.txt" in filtered.text
    assert "untagged.txt" not in filtered.text


def test_search_page_shows_tag_filter_options(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload(client, case_id)
    client.post(f"/documents/{document_id}/tags", data={"name": "IEP"})

    response = client.get(f"/cases/{case_id}/search")
    assert 'name="tag_id"' in response.text
    assert "IEP" in response.text
