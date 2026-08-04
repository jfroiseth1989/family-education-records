"""Tests for the pre-ingestion drop-zone preview endpoint and the
provenance tracking it feeds into ingestion (FERChronos document
drop-zone auto-fill).

Covers: strong/ambiguous/weak type & date matches from the preview
endpoint, that the preview never writes anything, provenance recorded on
the `imported` custody event, backward compatibility of the plain upload
flow with none of the new fields, the post-ingestion `document-date`
accept/override route, and original-file integrity.
"""

from __future__ import annotations

from pathlib import Path

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Document, DocumentCustodyEvent


def _make_pdf_bytes(text: str, tmp_path: Path) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    pdf_path = tmp_path / "scratch.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path.read_bytes()


def _create_case(client: TestClient, label: str = "Preview Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _preview(client: TestClient, case_id: int, filename: str, content: bytes):
    return client.post(
        f"/cases/{case_id}/documents/preview",
        files={"file": (filename, content, "text/plain")},
    )


def _upload(client: TestClient, case_id: int, filename: str, content: bytes, **extra):
    return client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, content, "text/plain")},
        data=extra,
        follow_redirects=False,
    )


# --- strong / ambiguous / weak matches via the preview endpoint ------------


def test_preview_strong_type_and_date_match(client: TestClient):
    case_id = _create_case(client)
    content = b"Individualized Education Program\nMeeting Date: 08/22/2023\nAttendees present."

    response = _preview(client, case_id, "notes.txt", content)
    assert response.status_code == 200
    payload = response.json()

    assert payload["document_type"] is not None
    assert payload["document_type"]["type_name"] == "IEP"
    assert payload["document_date"] is not None
    assert payload["document_date"]["value"] == "2023-08-22"
    assert payload["date_received"] is None


def test_regression_annual_iep_pdf_preview_request_and_response(client: TestClient, tmp_path: Path):
    """Regression for the reported bug: uploading a file named
    `IF 22-23 Annual IEP.pdf` left Document Type and dates blank. Exercises
    the full preview request/response path (not just the classifier
    directly) with a real PDF whose extractable text is exactly the kind
    of realistic annual-IEP boilerplate that used to trigger a false
    "ambiguous" result and suppress the type suggestion.
    """
    text = (
        "Individualized Education Program (IEP)\n"
        "Meeting Date: 08/22/2023\n"
        "Progress Report on prior IEP goals attached.\n"
        "Special Transportation Plan: door-to-door transportation required.\n"
        "Behavior Intervention Plan (BIP) reviewed and updated.\n"
        "Prior Written Notice of proposed changes to placement is included.\n"
        "North Crawford School District\n"
    )
    pdf_bytes = _make_pdf_bytes(text, tmp_path)
    case_id = _create_case(client)

    response = client.post(
        f"/cases/{case_id}/documents/preview",
        files={"file": ("IF 22-23 Annual IEP.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["document_type"] is not None
    assert payload["document_type"]["type_name"] == "IEP"
    assert payload["document_date"] is not None
    assert payload["document_date"]["value"] == "2023-08-22"
    assert payload["source"] is not None
    assert payload["source"]["value"] == "North Crawford School District"
    assert payload["notes"] is not None
    assert "IEP" in payload["notes"]["value"]


def test_regression_annual_iep_pdf_filename_alone_no_text(client: TestClient):
    """Same exact filename, but with no extractable text at all (e.g. a
    scanned image-only PDF, before OCR has run) -- Document Type must
    still come from the filename alone; dates correctly stay blank since
    nothing in an empty text sample supports one.
    """
    case_id = _create_case(client)
    # Not a real PDF -- the PDF extractor will fail to parse it and
    # _native_text_sample() falls back to "", exactly like an
    # unreadable/scanned file would before OCR completes.
    response = client.post(
        f"/cases/{case_id}/documents/preview",
        files={"file": ("IF 22-23 Annual IEP.pdf", b"not a real pdf", "application/pdf")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["document_type"] is not None
    assert payload["document_type"]["type_name"] == "IEP"
    assert payload["document_date"] is None


def test_preview_ambiguous_type_match_suggests_nothing(client: TestClient):
    case_id = _create_case(client)
    content = b"This covers both the Mediation Agreement and the Due Process Complaint filed."

    response = _preview(client, case_id, "combined.txt", content)
    assert response.status_code == 200
    payload = response.json()
    assert payload["document_type"] is None


def test_preview_weak_match_leaves_no_suggestion(client: TestClient):
    case_id = _create_case(client)
    content = b"Nothing relevant in this file at all."

    response = _preview(client, case_id, "random.txt", content)
    assert response.status_code == 200
    payload = response.json()
    assert payload["document_type"] is None
    assert payload["document_date"] is None
    assert payload["date_received"] is None
    assert payload["informational_dates"] == []


def test_preview_date_received_only_from_dedicated_anchor(client: TestClient):
    case_id = _create_case(client)
    content = b"This Prior Written Notice was provided to the parent on 05/06/2019 by mail."

    response = _preview(client, case_id, "pwn.txt", content)
    payload = response.json()
    assert payload["date_received"]["value"] == "2019-05-06"
    assert payload["document_date"] is None


def test_preview_unknown_case_returns_404(client: TestClient):
    response = _preview(client, 999999, "notes.txt", b"content")
    assert response.status_code == 404


# --- source and notes suggestions via the preview endpoint -----------------


def test_preview_suggests_source_from_school_district_name(client: TestClient):
    case_id = _create_case(client)
    content = b"This IEP was developed at North Crawford School District."

    response = _preview(client, case_id, "iep.txt", content)
    payload = response.json()
    assert payload["source"] is not None
    assert payload["source"]["value"] == "North Crawford School District"


def test_preview_conflicting_source_mentions_suggest_nothing(client: TestClient):
    case_id = _create_case(client)
    content = b"Issued by North Crawford School District. Also sent by Central Office."

    response = _preview(client, case_id, "iep.txt", content)
    payload = response.json()
    assert payload["source"] is None


def test_preview_composes_notes_summary_from_type_date_and_source(client: TestClient):
    content = (
        b"Individualized Education Program (IEP)\n"
        b"Meeting Date: 08/22/2023\n"
        b"IEP Implementation Date: 09/01/2023\n"
        b"North Crawford School District\n"
    )
    case_id = _create_case(client)

    response = _preview(client, case_id, "iep.txt", content)
    payload = response.json()
    assert payload["notes"] is not None
    assert payload["notes"]["value"] == (
        "IEP. Meeting date: 2023-08-22. "
        "Projected implementation date: 2023-09-01. North Crawford School District."
    )


def test_preview_notes_blank_when_nothing_supportable(client: TestClient):
    case_id = _create_case(client)
    content = b"Nothing relevant in this file at all."

    response = _preview(client, case_id, "random.txt", content)
    payload = response.json()
    assert payload["notes"] is None
    assert payload["source"] is None


# --- preview writes nothing / original-file integrity ----------------------


def test_preview_creates_no_document_row_or_custody_event(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _preview(client, case_id, "notes.txt", b"Meeting Date: 08/22/2023")

    with app.state.session_factory() as db:
        documents = db.scalars(select(Document).where(Document.case_id == case_id)).all()
    assert documents == []


def test_upload_after_preview_still_stores_original_bytes_unchanged(client: TestClient):
    case_id = _create_case(client)
    content = b"Meeting Date: 08/22/2023 exact original bytes"

    preview_response = _preview(client, case_id, "notes.txt", content)
    assert preview_response.status_code == 200

    upload_response = _upload(client, case_id, "notes.txt", content)
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    file_response = client.get(f"/documents/{document_id}/file")
    assert file_response.content == content


# --- provenance recording on ingestion --------------------------------------


def test_upload_with_no_provenance_fields_omits_field_provenance(client: TestClient, app: FastAPI):
    """Backward compatibility: an upload with none of the new *_source
    fields (the pre-existing form shape) must behave exactly as before --
    no field_provenance recorded at all.
    """
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "iep.txt", b"content")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        imported_event = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "imported",
            )
        ).one()
    assert imported_event.details is None


def test_upload_with_provenance_fields_records_them_on_imported_event(
    client: TestClient, app: FastAPI
):
    case_id = _create_case(client)
    upload_response = _upload(
        client,
        case_id,
        "iep.txt",
        b"Meeting Date: 08/22/2023",
        document_date="2023-08-22",
        document_date_source="suggested",
        date_received="2023-09-01",
        date_received_source="manual",
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        imported_event = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "imported",
            )
        ).one()

    assert imported_event.details["field_provenance"] == {
        "document_date": "suggested",
        "date_received": "manual",
    }


def test_upload_with_source_and_notes_provenance_recorded(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(
        client,
        case_id,
        "iep.txt",
        b"North Crawford School District",
        source="North Crawford School District",
        source_source="suggested",
        notes="IEP. North Crawford School District.",
        notes_source="edited",
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        imported_event = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "imported",
            )
        ).one()

    assert document.source == "North Crawford School District"
    assert document.notes == "IEP. North Crawford School District."
    assert imported_event.details["field_provenance"] == {
        "source": "suggested",
        "notes": "edited",
    }


def test_user_edited_value_is_recorded_as_edited_not_suggested(client: TestClient, app: FastAPI):
    """A user who changes a suggested value before submitting must have
    that recorded honestly -- the client-side JS is responsible for
    setting *_source to "edited" in that case; this test locks the
    server-side contract that whatever source string is submitted is
    stored verbatim, never silently coerced back to "suggested".
    """
    case_id = _create_case(client)
    upload_response = _upload(
        client,
        case_id,
        "iep.txt",
        b"content",
        document_date="2023-08-25",
        document_date_source="edited",
    )
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        imported_event = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "imported",
            )
        ).one()
        document = db.get(Document, document_id)

    assert imported_event.details["field_provenance"]["document_date"] == "edited"
    assert document.document_date.date().isoformat() == "2023-08-25"


def test_new_version_upload_records_date_provenance(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    v1_response = _upload(client, case_id, "iep-v1.txt", b"version one")
    v1_id = int(v1_response.headers["location"].rsplit("/", 1)[-1])

    v2_response = client.post(
        f"/documents/{v1_id}/new-version",
        files={"file": ("iep-v2.txt", b"version two", "text/plain")},
        data={
            "document_date": "2024-01-10",
            "document_date_source": "suggested",
            "date_received": "2024-01-15",
            "date_received_source": "manual",
        },
        follow_redirects=False,
    )
    v2_id = int(v2_response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        imported_event = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == v2_id,
                DocumentCustodyEvent.event_type == "imported",
            )
        ).one()

    assert imported_event.details["field_provenance"] == {
        "document_date": "suggested",
        "date_received": "manual",
    }


# --- deferred (post-ingestion) date suggestion display ----------------------


def test_document_detail_shows_deferred_date_suggestion_when_unset(client: TestClient):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "notes.txt", b"Meeting Date: 08/22/2023")

    response = client.get(upload_response.headers["location"])
    assert response.status_code == 200
    assert "Suggested document date" in response.text
    assert "2023-08-22" in response.text


def test_document_detail_hides_date_suggestion_once_date_is_set(client: TestClient):
    case_id = _create_case(client)
    upload_response = _upload(
        client, case_id, "notes.txt", b"Meeting Date: 08/22/2023", document_date="2023-08-22"
    )

    response = client.get(upload_response.headers["location"])
    assert response.status_code == 200
    assert "Suggested document date" not in response.text


# --- post-ingestion accept/override route -----------------------------------


def test_set_document_date_route_accepts_suggestion(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "notes.txt", b"content")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    response = client.post(
        f"/documents/{document_id}/document-date",
        data={
            "document_date": "2023-08-22",
            "document_date_precision": "exact",
            "document_date_source": "suggested",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        events = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "document_date_set",
            )
        ).all()

    assert document.document_date.date().isoformat() == "2023-08-22"
    assert document.document_date_source == "extracted"
    assert len(events) == 1
    assert events[0].details["document_date"]["source"] == "suggested"


def test_set_document_date_route_manual_entry_recorded_as_manual(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "notes.txt", b"content")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    client.post(
        f"/documents/{document_id}/document-date",
        data={"document_date": "2023-08-22", "document_date_precision": "exact"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)

    assert document.document_date_source == "manual"


def test_set_document_date_route_resubmitting_same_value_writes_no_event(
    client: TestClient, app: FastAPI
):
    case_id = _create_case(client)
    upload_response = _upload(client, case_id, "notes.txt", b"content", document_date="2023-08-22")
    document_id = int(upload_response.headers["location"].rsplit("/", 1)[-1])

    client.post(
        f"/documents/{document_id}/document-date",
        data={"document_date": "2023-08-22", "document_date_precision": "exact"},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        events = db.scalars(
            select(DocumentCustodyEvent).where(
                DocumentCustodyEvent.document_id == document_id,
                DocumentCustodyEvent.event_type == "document_date_set",
            )
        ).all()
    assert events == []


# --- no-network guard --------------------------------------------------------


def test_preview_route_never_calls_ocr_or_background_jobs(client: TestClient, app: FastAPI):
    """The preview endpoint must only ever use native text extraction --
    never enqueue or run OCR (a background-worker job, architecturally
    unsafe to run synchronously inside a request) -- for an image file
    with no native text layer at all.
    """
    from app.db.models import OcrJob

    case_id = _create_case(client)
    response = _preview(client, case_id, "scan.png", b"not a real png but that's fine")
    assert response.status_code == 200
    payload = response.json()
    assert payload["document_type"] is None

    with app.state.session_factory() as db:
        jobs = db.scalars(select(OcrJob)).all()
    assert jobs == []


def test_document_preview_js_makes_no_external_network_calls():
    """Static guard: the client-side preview script must only ever fetch
    this application's own relative preview URL -- no absolute http(s)://
    URL, third-party domain, or CDN reference of any kind.
    """
    script_path = (
        Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "document_preview.js"
    )
    source = script_path.read_text()
    assert "http://" not in source
    assert "https://" not in source
