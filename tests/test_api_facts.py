"""Tests for POST /documents/{document_id}/facts/scan-dates (Phase 3.5 Step 3)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AiObservation, AuditLog, Document, DocumentPage


def _create_case(client: TestClient, label: str = "Date Scan Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_scan_dates_creates_observations_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("letter.txt", b"The meeting was held on March 12, 2024.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    scan_response = client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)

    assert scan_response.status_code == 303
    assert scan_response.headers["location"] == f"/documents/{document_id}?date_scan=1"

    with app.state.session_factory() as db:
        observations = db.scalars(select(AiObservation)).all()
        assert len(observations) == 1
        assert observations[0].statement == "Possible date: 2024-03-12"

        audit_entries = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "date_scan_triggered")
        ).all()
        assert len(audit_entries) == 1
        assert audit_entries[0].details["observations_created"] == 1


def test_scan_dates_is_safe_to_run_twice(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("letter.txt", b"Reviewed 2024-06-01.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    first = client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)
    second = client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)

    assert first.headers["location"] == f"/documents/{document_id}?date_scan=1"
    assert second.headers["location"] == f"/documents/{document_id}?date_scan=0"

    with app.state.session_factory() as db:
        observations = db.scalars(select(AiObservation)).all()
        assert len(observations) == 1


def test_scan_dates_nonexistent_document_returns_404(client: TestClient):
    response = client.post("/documents/99999/facts/scan-dates", follow_redirects=False)
    assert response.status_code == 404


def test_document_detail_shows_scan_button_and_result(client: TestClient):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("letter.txt", b"Signed January 1, 2024.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    detail = client.get(f"/documents/{document_id}")
    assert "Scan for date observations" in detail.text

    scan = client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=True)
    assert "new possible date" in scan.text
