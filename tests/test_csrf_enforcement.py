"""Application-wide CSRF enforcement (Security Phase Step 3.5).

`AuthEnforcementMiddleware` (app/core/auth/enforcement.py) validates a
CSRF token on every mutating request (POST/PUT/PATCH/DELETE) outside
`/auth/*`, centrally -- see that module's docstring and
app/core/auth/csrf.py's `extract_submitted_csrf_token()`. This file
proves that for one representative route from every functional area:
missing and invalid tokens are rejected (with no database or file
mutation happening first), and a valid token -- submitted either as the
`csrf_token` form field every HTML form in this app now carries, or as
the `X-CSRF-Token` header documented for non-browser clients -- succeeds.

Uses `client_no_csrf_header` (see tests/conftest.py): a genuinely
logged-in client that, unlike the suite-wide `client` fixture, does not
have the CSRF header pre-set, so each test here controls exactly what
token (if any) a request carries.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Annotation, Case, Citation, Document, DocumentPage, Tag, VerifiedFact


def _csrf(client: TestClient) -> str:
    return client.cookies.get("csrf_token")


def _post_with_csrf(client: TestClient, url: str, data: dict | None = None, **kwargs):
    body = dict(data or {})
    body["csrf_token"] = _csrf(client)
    return client.post(url, data=body, follow_redirects=False, **kwargs)


def _create_case(client: TestClient, label: str = "CSRF Enforcement Test Case") -> int:
    response = _post_with_csrf(client, "/cases", {"label": label})
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_text(client: TestClient, case_id: int, filename: str = "doc.txt", content: bytes = b"Hello evidence text.") -> int:
    body = {"csrf_token": _csrf(client)}
    response = client.post(
        f"/cases/{case_id}/documents",
        data=body,
        files={"file": (filename, content, "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _get_page_id(app: FastAPI, document_id: int) -> int:
    with app.state.session_factory() as db:
        return db.scalars(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
        ).one().page_id


# --- Students / Cases -------------------------------------------------


def test_create_case_missing_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    response = client_no_csrf_header.post("/cases", data={"label": "Should Not Exist"}, follow_redirects=False)
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Case).where(Case.label == "Should Not Exist")).first() is None


def test_create_case_invalid_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    response = client_no_csrf_header.post(
        "/cases",
        data={"label": "Should Not Exist Either", "csrf_token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Case).where(Case.label == "Should Not Exist Either")).first() is None


def test_create_case_valid_csrf_token_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header, "A Real Student")
    with app.state.session_factory() as db:
        assert db.get(Case, case_id) is not None


# --- Documents (multipart upload) --------------------------------------


def test_document_upload_missing_csrf_token_rejected_before_any_file_is_stored(
    client_no_csrf_header: TestClient, app: FastAPI
):
    case_id = _create_case(client_no_csrf_header)

    response = client_no_csrf_header.post(
        f"/cases/{case_id}/documents",
        files={"file": ("evidence.txt", b"should never be stored", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Document).where(Document.case_id == case_id)).all() == []


def test_document_upload_invalid_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)

    response = client_no_csrf_header.post(
        f"/cases/{case_id}/documents",
        data={"csrf_token": "wrong-token"},
        files={"file": ("evidence.txt", b"should never be stored", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Document).where(Document.case_id == case_id)).all() == []


def test_document_upload_valid_csrf_token_in_form_field_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)

    with app.state.session_factory() as db:
        assert db.get(Document, document_id) is not None


def test_document_upload_valid_csrf_token_via_header_succeeds_with_no_form_field(
    client_no_csrf_header: TestClient, app: FastAPI
):
    """The documented non-browser path: a script can send the token as the
    `X-CSRF-Token` header instead of multipart-encoding a `csrf_token`
    field alongside the file. This client normally has no default header
    (see `client_no_csrf_header`'s docstring) -- setting it just for this
    one request via `.post(..., headers=...)` proves the header path
    itself, independent of the suite-wide `client` fixture that always
    carries it.
    """
    case_id = _create_case(client_no_csrf_header)

    response = client_no_csrf_header.post(
        f"/cases/{case_id}/documents",
        headers={"X-CSRF-Token": _csrf(client_no_csrf_header)},
        files={"file": ("header-path.txt", b"uploaded via header-based csrf token", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with app.state.session_factory() as db:
        assert db.scalars(select(Document).where(Document.case_id == case_id)).all() != []


# --- Tags ----------------------------------------------------------------


def test_add_tag_missing_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)

    response = client_no_csrf_header.post(
        f"/documents/{document_id}/tags", data={"name": "Should Not Be Added"}, follow_redirects=False
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Tag).where(Tag.name == "Should Not Be Added")).first() is None


def test_add_tag_invalid_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)

    response = client_no_csrf_header.post(
        f"/documents/{document_id}/tags",
        data={"name": "Still Should Not Be Added", "csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Tag).where(Tag.name == "Still Should Not Be Added")).first() is None


def test_add_tag_valid_csrf_token_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)

    response = _post_with_csrf(client_no_csrf_header, f"/documents/{document_id}/tags", {"name": "IEP"})
    assert response.status_code == 303

    with app.state.session_factory() as db:
        assert db.scalars(select(Tag).where(Tag.name == "IEP")).first() is not None


# --- Annotations -----------------------------------------------------------


def test_add_note_missing_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)
    page_id = _get_page_id(app, document_id)

    response = client_no_csrf_header.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Should not be saved"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Annotation).where(Annotation.document_id == document_id)).all() == []


def test_add_note_invalid_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)
    page_id = _get_page_id(app, document_id)

    response = client_no_csrf_header.post(
        f"/documents/{document_id}/annotations/note",
        data={"page_id": page_id, "body_text": "Still should not be saved", "csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as db:
        assert db.scalars(select(Annotation).where(Annotation.document_id == document_id)).all() == []


def test_add_note_valid_csrf_token_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id)
    page_id = _get_page_id(app, document_id)

    response = _post_with_csrf(
        client_no_csrf_header,
        f"/documents/{document_id}/annotations/note",
        {"page_id": page_id, "body_text": "A real note"},
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        assert len(db.scalars(select(Annotation).where(Annotation.document_id == document_id)).all()) == 1


# --- Facts -----------------------------------------------------------------


def test_scan_dates_missing_csrf_token_rejected(client_no_csrf_header: TestClient):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id, content=b"Evaluation held 2024-01-15.")

    response = client_no_csrf_header.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)
    assert response.status_code == 403


def test_scan_dates_invalid_csrf_token_rejected(client_no_csrf_header: TestClient):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id, content=b"Evaluation held 2024-01-15.")

    response = client_no_csrf_header.post(
        f"/documents/{document_id}/facts/scan-dates",
        data={"csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_scan_dates_valid_csrf_token_succeeds(client_no_csrf_header: TestClient):
    case_id = _create_case(client_no_csrf_header)
    document_id = _upload_text(client_no_csrf_header, case_id, content=b"Evaluation held 2024-01-15.")

    response = _post_with_csrf(client_no_csrf_header, f"/documents/{document_id}/facts/scan-dates")
    assert response.status_code == 303


# --- Timeline ----------------------------------------------------------


def test_create_timeline_event_missing_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    date_fact_id = _create_date_fact(client_no_csrf_header, app, case_id)

    response = client_no_csrf_header.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "Should not be created", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_timeline_event_invalid_csrf_token_rejected(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    date_fact_id = _create_date_fact(client_no_csrf_header, app, case_id)

    response = client_no_csrf_header.post(
        f"/cases/{case_id}/timeline",
        data={
            "event_type": "meeting",
            "title": "Still should not be created",
            "date_fact_id": date_fact_id,
            "csrf_token": "wrong-token",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_timeline_event_valid_csrf_token_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    case_id = _create_case(client_no_csrf_header)
    date_fact_id = _create_date_fact(client_no_csrf_header, app, case_id)

    response = _post_with_csrf(
        client_no_csrf_header,
        f"/cases/{case_id}/timeline",
        {"event_type": "meeting", "title": "A real timeline event", "date_fact_id": date_fact_id},
    )
    assert response.status_code == 303


def _create_date_fact(client: TestClient, app: FastAPI, case_id: int, fact_date: str = "2024-01-15") -> int:
    document_id = _upload_text(client, case_id, "date-doc.txt", b"Some page content for a fact.")
    page_id = _get_page_id(app, document_id)

    _post_with_csrf(
        client,
        f"/documents/{document_id}/annotations/highlight",
        {"page_id": page_id, "start_offset": 0, "end_offset": 4},
    )

    with app.state.session_factory() as db:
        citation_id = db.scalars(select(Citation).where(Citation.document_id == document_id)).one().citation_id

    response = _post_with_csrf(
        client,
        f"/documents/{document_id}/facts/create-from-citation",
        {
            "citation_id": citation_id,
            "fact_type": "date",
            "statement": "A date fact for timeline tests",
            "confidence_label": "certain",
            "fact_date": fact_date,
            "page": 1,
        },
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        return db.scalars(
            select(VerifiedFact).where(VerifiedFact.statement == "A date fact for timeline tests")
        ).one().fact_id


# --- OCR ---------------------------------------------------------------


def test_ocr_correction_missing_csrf_token_rejected(client_no_csrf_header: TestClient):
    # Deliberately targets a document/page that doesn't exist -- the CSRF
    # check in AuthEnforcementMiddleware runs before routing/dependency
    # resolution ever looks the document up, so this still proves the
    # rejection happens before any handler-level mutation, without needing
    # a full OCR job pipeline set up just for the negative-path tests.
    response = client_no_csrf_header.post(
        "/documents/999999/ocr-review/pages/1/correct",
        data={"corrected_text": "should not be saved"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_ocr_correction_invalid_csrf_token_rejected(client_no_csrf_header: TestClient):
    response = client_no_csrf_header.post(
        "/documents/999999/ocr-review/pages/1/correct",
        data={"corrected_text": "still should not be saved", "csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_ocr_correction_valid_csrf_token_succeeds(client_no_csrf_header: TestClient, app: FastAPI):
    from unittest import mock

    from app.core.ocr.engine import OcrResult
    from app.db.models import OcrCorrection
    from app.jobs.worker import process_next_job

    case_id = _create_case(client_no_csrf_header)
    upload_response = client_no_csrf_header.post(
        f"/cases/{case_id}/documents",
        data={"csrf_token": _csrf(client_no_csrf_header)},
        files={"file": ("scan.jpg", b"\xff\xd8\xff\xe0fake-jpeg-bytes", "image/jpeg")},
        follow_redirects=False,
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    fake_result = OcrResult(text="raw ocr text", confidence=60.0, word_boxes=[])
    with mock.patch("app.core.ocr.service.is_tesseract_available", return_value=True), \
         mock.patch("app.core.ocr.service.engine_label", return_value="tesseract-test"), \
         mock.patch("app.core.ocr.service.run_ocr_on_image", return_value=fake_result):
        with app.state.session_factory() as db:
            process_next_job(db, app.state.vault)

    response = _post_with_csrf(
        client_no_csrf_header,
        f"/documents/{document_id}/ocr-review/pages/1/correct",
        {"corrected_text": "a real correction"},
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        page_id = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one().page_id
        assert db.scalars(select(OcrCorrection).where(OcrCorrection.page_id == page_id)).first() is not None


# --- Cache headers on a CSRF rejection response -----------------------


def test_csrf_rejection_response_has_no_store_cache_headers(client_no_csrf_header: TestClient):
    response = client_no_csrf_header.post("/cases", data={"label": "irrelevant"}, follow_redirects=False)
    assert response.status_code == 403
    assert response.headers.get("Cache-Control") == "no-store, private"
