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


def test_create_case_accepts_optional_name_fields(client: TestClient, app: FastAPI):
    response = client.post(
        "/cases",
        data={
            "label": "Isabella Froiseth",
            "legal_first_name": "Isabella",
            "legal_last_name": "Froiseth",
            "preferred_name": "Izzy",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    case = _first_case(app)
    assert case.legal_first_name == "Isabella"
    assert case.legal_last_name == "Froiseth"
    assert case.preferred_name == "Izzy"
    assert case.display_name == "Isabella (Izzy) Froiseth"


def test_create_case_without_name_fields_falls_back_to_label(client: TestClient, app: FastAPI):
    """Every existing caller that only ever sent label/description (the
    entire pre-Step-2 test suite) must keep working unchanged.
    """
    response = client.post("/cases", data={"label": "Zeke Froiseth"}, follow_redirects=False)
    assert response.status_code == 303

    case = _first_case(app)
    assert case.legal_first_name is None
    assert case.legal_last_name is None
    assert case.preferred_name is None
    assert case.display_name == "Zeke Froiseth"


def test_edit_case_updates_name_fields(client: TestClient, app: FastAPI):
    create_response = client.post(
        "/cases", data={"label": "Name Edit Test"}, follow_redirects=False
    )
    case_id = create_response.headers["location"].rsplit("/", 1)[-1]

    response = client.post(
        f"/cases/{case_id}/edit",
        data={
            "label": "Name Edit Test",
            "description": "",
            "status": "active",
            "legal_first_name": "First",
            "legal_last_name": "Last",
            "preferred_name": "Nick",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        case = db.get(Case, int(case_id))
        assert case.display_name == "First (Nick) Last"


def test_case_list_shows_full_display_name_format(client: TestClient):
    client.post(
        "/cases",
        data={
            "label": "Isabella Froiseth",
            "legal_first_name": "Isabella",
            "legal_last_name": "Froiseth",
            "preferred_name": "Izzy",
        },
    )
    response = client.get("/cases")
    assert "Isabella (Izzy) Froiseth" in response.text
    assert "legal:" not in response.text.lower()


def test_case_detail_heading_shows_full_display_name_format(client: TestClient):
    create_response = client.post(
        "/cases",
        data={
            "label": "Isabella Froiseth",
            "legal_first_name": "Isabella",
            "legal_last_name": "Froiseth",
            "preferred_name": "Izzy",
        },
        follow_redirects=False,
    )
    response = client.get(create_response.headers["location"])
    assert "Isabella (Izzy) Froiseth" in response.text


def test_student_selector_present_on_case_scoped_page(client: TestClient):
    create_response = client.post(
        "/cases", data={"label": "Selector Test Student"}, follow_redirects=False
    )
    response = client.get(create_response.headers["location"])
    assert "student-selector" in response.text
    assert "+ Add Student" in response.text
    assert "Selector Test Student" in response.text


def test_student_selector_lists_all_students_and_highlights_active_one(client: TestClient):
    first = client.post("/cases", data={"label": "First Student"}, follow_redirects=False)
    second = client.post("/cases", data={"label": "Second Student"}, follow_redirects=False)

    response = client.get(first.headers["location"])
    assert "First Student" in response.text
    assert "Second Student" in response.text  # both listed for switching

    second_id = second.headers["location"].rsplit("/", 1)[-1]
    assert f'href="/cases/{second_id}"' in response.text


def test_student_selector_shows_no_active_student_on_list_page(client: TestClient):
    client.post("/cases", data={"label": "Some Student"})
    response = client.get("/cases")
    assert "Select a student" in response.text


def test_case_detail_shows_drag_and_drop_upload_zone(client: TestClient):
    """FERChronos UX refinement Step 3: front-end drop zone only -- the
    underlying <input type="file" name="file" required> that the upload
    route actually reads must be unchanged, or ingestion silently breaks.
    """
    create_response = client.post(
        "/cases", data={"label": "Dropzone Test Student"}, follow_redirects=False
    )
    response = client.get(create_response.headers["location"])

    assert "Drag and drop a document here" in response.text
    assert "or click to browse" in response.text
    assert 'id="dropzone"' in response.text
    assert 'name="file"' in response.text
    assert "required" in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert 'action="/cases/' in response.text and "/documents" in response.text
    assert "Uploading for" in response.text
    assert "Dropzone Test Student" in response.text
    assert '/static/dropzone.js' in response.text


def test_dropzone_script_is_served(client: TestClient):
    response = client.get("/static/dropzone.js")
    assert response.status_code == 200
    assert "dataTransfer" in response.text
