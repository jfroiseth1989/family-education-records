"""End-to-end tests for the search route/UI, via the FastAPI TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_case(client: TestClient, label: str = "Search API Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload(client: TestClient, case_id: int, filename: str, content: bytes, **extra):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        data=extra,
        follow_redirects=False,
    )


def test_search_page_renders_without_a_query(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/search")
    assert response.status_code == 200
    assert "Results" not in response.text  # no results section without a query


def test_search_finds_uploaded_document(client: TestClient):
    case_id = _create_case(client)
    _upload(client, case_id, "notes.txt", b"The evaluation mentioned a specific accommodation.")

    response = client.get(f"/cases/{case_id}/search", params={"q": "accommodation"})
    assert response.status_code == 200
    assert "notes.txt" in response.text
    assert "Results (1)" in response.text


def test_search_no_results_message(client: TestClient):
    case_id = _create_case(client)
    _upload(client, case_id, "notes.txt", b"Some unrelated content entirely.")

    response = client.get(f"/cases/{case_id}/search", params={"q": "zzz_no_such_word"})
    assert "No matches found" in response.text


def test_search_is_scoped_to_the_requested_case(client: TestClient):
    case_a = _create_case(client, "Case A")
    case_b = _create_case(client, "Case B")
    _upload(client, case_a, "a.txt", b"unique marker word appears here")
    _upload(client, case_b, "b.txt", b"unique marker word appears here too")

    response = client.get(f"/cases/{case_a}/search", params={"q": "unique marker"})
    assert "a.txt" in response.text
    assert "b.txt" not in response.text


def test_search_invalid_date_returns_400(client: TestClient):
    case_id = _create_case(client)
    response = client.get(
        f"/cases/{case_id}/search", params={"q": "test", "date_from": "not-a-date"}
    )
    assert response.status_code == 400


def test_search_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/search", params={"q": "test"})
    assert response.status_code == 404


def test_search_result_snippet_is_html_escaped(client: TestClient):
    """A document's own text could coincidentally (or adversarially)
    contain characters that look like HTML -- the snippet must render as
    literal text, not be interpreted as markup. This locks in a real fix
    made during implementation (an initial draft used `| safe` in the
    template, which would have been a stored-XSS-via-document-content
    risk; it was removed in favor of Jinja2's default auto-escaping).
    """
    case_id = _create_case(client)
    _upload(
        client,
        case_id,
        "notes.txt",
        b"This document mentions marker <script>alert(1)</script> right here.",
    )

    response = client.get(f"/cases/{case_id}/search", params={"q": "marker"})
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    # The escaped form should appear instead (proving the text was found
    # and rendered, just safely).
    assert "&lt;script&gt;" in response.text


def test_search_link_appears_on_case_detail_page(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert f"/cases/{case_id}/search" in response.text
