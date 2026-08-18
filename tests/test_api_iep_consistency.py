"""HTTP-level tests for app/api/iep_consistency.py (IEP Consistency
Review Step 2): the scan route, the manual-entry route, and
exclude/restore -- mirrors tests/test_api_facts_review.py's structure
and its use of the `client`/`app` fixtures (CSRF handled automatically
by the `client` fixture).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Document, DocumentPage, IepRecord


def _create_case(client: TestClient, label: str = "IEP Consistency Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_document(client: TestClient, case_id: int, text: str, filename: str = "iep.txt") -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, text.encode(), "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


# --- scan route --------------------------------------------------------


def test_scan_route_creates_service_record_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy — 30 minutes, 2x/week")

    response = client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}?iep_scan_found=1"

    with app.state.session_factory() as db:
        record = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).one()
        assert record.extraction_method == "iep-service-line-regex-v1"

        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_service_scan_triggered")
        ).one()
        assert audit.entity_id == document_id
        assert audit.details["records_created"] == 1


def test_scan_route_is_idempotent(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy — 30 minutes, 2x/week")

    client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)
    second = client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)

    assert second.headers["location"] == f"/documents/{document_id}?iep_scan_found=0"
    with app.state.session_factory() as db:
        records = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).all()
        assert len(records) == 1


def test_scan_route_nonexistent_document_returns_404(client: TestClient):
    response = client.post("/documents/99999/iep-records/scan", follow_redirects=False)
    assert response.status_code == 404


def test_document_detail_page_shows_scanned_service_record(client: TestClient):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy — 30 minutes, 2x/week")
    client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)

    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert "Speech-language therapy" in response.text
    assert "Extracted structured data" in response.text


# --- manual-entry route --------------------------------------------------


def _page_id_for(app: FastAPI, document_id: int) -> int:
    with app.state.session_factory() as db:
        page = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one()
        return page.page_id


def test_manual_entry_creates_record_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    text = "Occupational therapy details on this page."
    document_id = _upload_document(client, case_id, text)
    page_id = _page_id_for(app, document_id)

    response = client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": len("Occupational therapy"),
            "service_name": "Occupational therapy",
            "minutes": "30",
            "frequency_count": "2",
            "frequency_period": "week",
            "location": "the gym",
            "provider": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{document_id}/view?page=1"

    with app.state.session_factory() as db:
        record = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).one()
        assert record.extraction_method == "manual"
        values = {f.field_type.name: f for f in record.fields}
        assert values["service_name"].text_value == "Occupational therapy"
        assert values["minutes"].numeric_value == 30.0
        assert "provider" not in values

        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_record_added_manually")
        ).one()
        assert audit.entity_id == record.record_id


def test_manual_entry_with_only_service_name(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    text = "Counseling notes on this page."
    document_id = _upload_document(client, case_id, text)
    page_id = _page_id_for(app, document_id)

    response = client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": len("Counseling"),
            "service_name": "Counseling",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        record = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).one()
        assert len(record.fields) == 1


def test_manual_entry_invalid_range_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Short page text.")
    page_id = _page_id_for(app, document_id)

    response = client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": 9999,
            "service_name": "OT",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_manual_entry_empty_service_name_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Short page text.")
    page_id = _page_id_for(app, document_id)

    response = client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": 5,
            "service_name": "   ",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_manual_entry_non_numeric_minutes_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Short page text.")
    page_id = _page_id_for(app, document_id)

    response = client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": 5,
            "service_name": "OT",
            "minutes": "not-a-number",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_manual_entry_page_from_different_document_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_a = _upload_document(client, case_id, "Document A page text.", filename="a.txt")
    document_b = _upload_document(client, case_id, "Document B page text.", filename="b.txt")
    page_id_b = _page_id_for(app, document_b)

    response = client.post(
        f"/documents/{document_a}/iep-records/service",
        data={
            "page_id": page_id_b,
            "start_offset": 0,
            "end_offset": 5,
            "service_name": "OT",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_manual_entry_nonexistent_document_returns_404(client: TestClient):
    response = client.post(
        "/documents/99999/iep-records/service",
        data={"page_id": 1, "start_offset": 0, "end_offset": 5, "service_name": "OT"},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- exclude / restore -----------------------------------------------------


def test_exclude_and_restore_record(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy — 30 minutes, 2x/week")
    client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)

    with app.state.session_factory() as db:
        record_id = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).one().record_id

    exclude_response = client.post(f"/iep-records/{record_id}/exclude", follow_redirects=False)
    assert exclude_response.status_code == 303
    assert exclude_response.headers["location"] == f"/documents/{document_id}"

    with app.state.session_factory() as db:
        record = db.get(IepRecord, record_id)
        assert record.status == "excluded"
        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_record_excluded")
        ).one()
        assert audit.entity_id == record_id

    restore_response = client.post(f"/iep-records/{record_id}/restore", follow_redirects=False)
    assert restore_response.status_code == 303

    with app.state.session_factory() as db:
        record = db.get(IepRecord, record_id)
        assert record.status == "active"
        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_record_restored")
        ).one()
        assert audit.entity_id == record_id


def test_excluded_record_still_shown_with_restore_action(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy — 30 minutes, 2x/week")
    client.post(f"/documents/{document_id}/iep-records/scan", follow_redirects=False)

    with app.state.session_factory() as db:
        record_id = db.scalars(select(IepRecord).where(IepRecord.document_id == document_id)).one().record_id

    client.post(f"/iep-records/{record_id}/exclude", follow_redirects=False)

    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert "Speech-language therapy" in response.text


def test_exclude_nonexistent_record_returns_404(client: TestClient):
    response = client.post("/iep-records/99999/exclude", follow_redirects=False)
    assert response.status_code == 404


def test_restore_nonexistent_record_returns_404(client: TestClient):
    response = client.post("/iep-records/99999/restore", follow_redirects=False)
    assert response.status_code == 404
