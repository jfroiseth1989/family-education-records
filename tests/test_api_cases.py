"""End-to-end tests for the case CRUD routes, via the FastAPI TestClient."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Case


def _first_case(app: FastAPI) -> Case:
    with app.state.session_factory() as db:
        return db.scalars(select(Case)).first()


def test_create_case_redirects_to_detail_page(client: TestClient):
    response = client.post(
        "/cases",
        data={"label": "Jane Doe — IEP Dispute", "description": "A test case."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/cases/")


def test_create_case_requires_a_label(client: TestClient):
    response = client.post("/cases", data={"label": "   "})
    assert response.status_code == 400


def test_case_list_shows_created_case(client: TestClient):
    client.post("/cases", data={"label": "Visible Case"})
    response = client.get("/cases")
    assert response.status_code == 200
    assert "Visible Case" in response.text


def test_case_detail_page_renders(client: TestClient):
    create_response = client.post(
        "/cases", data={"label": "Detail Case"}, follow_redirects=False
    )
    location = create_response.headers["location"]

    response = client.get(location)
    assert response.status_code == 200
    assert "Detail Case" in response.text


def test_get_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/9999")
    assert response.status_code == 404


def test_create_case_writes_audit_log_entry(client: TestClient, app: FastAPI):
    client.post("/cases", data={"label": "Audited Case"})

    with app.state.session_factory() as db:
        entries = db.scalars(select(AuditLog).where(AuditLog.event_type == "case_created")).all()
    assert len(entries) == 1
    assert entries[0].details["label"] == "Audited Case"


def test_edit_case_updates_fields_and_logs_audit_entry(client: TestClient, app: FastAPI):
    create_response = client.post(
        "/cases", data={"label": "Original Label"}, follow_redirects=False
    )
    case_id = create_response.headers["location"].rsplit("/", 1)[-1]

    response = client.post(
        f"/cases/{case_id}/edit",
        data={"label": "Updated Label", "description": "Now with a description", "status": "closed"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        case = db.get(Case, int(case_id))
        assert case.label == "Updated Label"
        assert case.description == "Now with a description"
        assert case.status == "closed"

        audit_entries = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "case_edited")
        ).all()
    assert len(audit_entries) == 1
    assert audit_entries[0].details["label"]["new"] == "Updated Label"


def test_edit_case_rejects_invalid_status(client: TestClient):
    create_response = client.post(
        "/cases", data={"label": "Status Test"}, follow_redirects=False
    )
    case_id = create_response.headers["location"].rsplit("/", 1)[-1]

    response = client.post(
        f"/cases/{case_id}/edit",
        data={"label": "Status Test", "description": "", "status": "not-a-real-status"},
    )
    assert response.status_code == 400
