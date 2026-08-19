"""HTTP-level tests for IEP Consistency Review Step 3: the case-level
comparison scan, the Consistency Review page, and the confirm/dismiss/
note flag-lifecycle routes. Mirrors tests/test_api_iep_consistency.py's
structure and its use of the `client`/`app` fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Document, IepInconsistencyFlag


def _create_case(client: TestClient, label: str = "Consistency Step 3 Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_document(client: TestClient, case_id: int, text: str, filename: str = "iep.txt") -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, text.encode(), "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _add_two_conflicting_service_records(client: TestClient, app: FastAPI, document_id: int) -> None:
    """Two manually-entered service records for the same normalized
    service name with different minutes -- the mockup scenario.
    """
    with app.state.session_factory() as db:
        from app.db.models import DocumentPage

        page_id = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one().page_id

    client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 0,
            "end_offset": 5,
            "service_name": "Speech-language therapy",
            "minutes": "30",
        },
        follow_redirects=False,
    )
    client.post(
        f"/documents/{document_id}/iep-records/service",
        data={
            "page_id": page_id,
            "start_offset": 5,
            "end_offset": 10,
            "service_name": "Speech-language therapy",
            "minutes": "20",
        },
        follow_redirects=False,
    )


# --- case-level scan --------------------------------------------------


def test_scan_case_creates_flag_and_redirects(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)

    response = client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case_id}/consistency?scan_found=1"

    with app.state.session_factory() as db:
        flag = db.scalars(select(IepInconsistencyFlag).where(IepInconsistencyFlag.case_id == case_id)).one()
        assert flag.status == "pending"

        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_inconsistency_scan_triggered")
        ).one()
        assert audit.details["flags_created"] == 1


def test_scan_case_is_idempotent(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)

    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    second = client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)

    assert second.headers["location"] == f"/cases/{case_id}/consistency?scan_found=0"
    with app.state.session_factory() as db:
        flags = db.scalars(select(IepInconsistencyFlag).where(IepInconsistencyFlag.case_id == case_id)).all()
        assert len(flags) == 1


def test_scan_case_skips_soft_deleted_documents(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)

    with app.state.session_factory() as db:
        document = db.get(Document, document_id)
        document.deleted_at = datetime.now(timezone.utc)
        db.commit()

    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)

    with app.state.session_factory() as db:
        flags = db.scalars(select(IepInconsistencyFlag).where(IepInconsistencyFlag.case_id == case_id)).all()
        assert flags == []


def test_scan_case_nonexistent_case_returns_404(client: TestClient):
    response = client.post("/cases/99999/consistency/scan", follow_redirects=False)
    assert response.status_code == 404


# --- consistency review page --------------------------------------------------


def test_consistency_review_page_lists_pending_flag(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)

    response = client.get(f"/cases/{case_id}/consistency")

    assert response.status_code == 200
    assert "Possible service inconsistency" in response.text
    assert "do not match" in response.text
    assert "Confirm inconsistency" in response.text
    assert "Mark not an inconsistency" in response.text

    # The legal-boundary rule applies to machine-generated flag content
    # (reason_text) -- not this page's own static policy-explanation
    # copy, which may reference these terms to describe what the tool
    # deliberately never asserts.
    with app.state.session_factory() as db:
        flag = db.scalars(select(IepInconsistencyFlag).where(IepInconsistencyFlag.case_id == case_id)).one()
        reason = flag.reason_text.lower()
        for banned_term in ("violation", "illegal", "noncompliant", "denial of fape"):
            assert banned_term not in reason


def test_consistency_review_page_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/consistency")
    assert response.status_code == 404


def test_consistency_review_page_empty_state(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/consistency")
    assert response.status_code == 200
    assert "No pending flags" in response.text


# --- confirm / dismiss / note --------------------------------------------------


def _get_flag_id(app: FastAPI, case_id: int) -> int:
    with app.state.session_factory() as db:
        return db.scalars(
            select(IepInconsistencyFlag).where(IepInconsistencyFlag.case_id == case_id)
        ).one().flag_id


def test_confirm_flag_route(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    flag_id = _get_flag_id(app, case_id)

    response = client.post(f"/cases/{case_id}/consistency/flags/{flag_id}/confirm", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case_id}/consistency"

    with app.state.session_factory() as db:
        flag = db.get(IepInconsistencyFlag, flag_id)
        assert flag.status == "confirmed"
        assert flag.reviewed_by is not None
        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_inconsistency_flag_confirmed")
        ).one()
        assert audit.entity_id == flag_id


def test_dismiss_flag_route(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    flag_id = _get_flag_id(app, case_id)

    response = client.post(f"/cases/{case_id}/consistency/flags/{flag_id}/dismiss", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        flag = db.get(IepInconsistencyFlag, flag_id)
        assert flag.status == "dismissed"
        audit = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "iep_inconsistency_flag_dismissed")
        ).one()
        assert audit.entity_id == flag_id


def test_confirm_then_dismiss_is_reversible_via_routes(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    flag_id = _get_flag_id(app, case_id)

    client.post(f"/cases/{case_id}/consistency/flags/{flag_id}/confirm", follow_redirects=False)
    client.post(f"/cases/{case_id}/consistency/flags/{flag_id}/dismiss", follow_redirects=False)

    with app.state.session_factory() as db:
        assert db.get(IepInconsistencyFlag, flag_id).status == "dismissed"


def test_set_note_route(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    flag_id = _get_flag_id(app, case_id)

    response = client.post(
        f"/cases/{case_id}/consistency/flags/{flag_id}/note",
        data={"note_text": "Confirmed with the district coordinator."},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        flag = db.get(IepInconsistencyFlag, flag_id)
        assert flag.user_note == "Confirmed with the district coordinator."
        assert flag.status == "pending"

    review_page = client.get(f"/cases/{case_id}/consistency")
    assert "Confirmed with the district coordinator." in review_page.text


def test_flag_route_wrong_case_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    document_id = _upload_document(client, case_id, "Speech-language therapy details here for testing.")
    _add_two_conflicting_service_records(client, app, document_id)
    client.post(f"/cases/{case_id}/consistency/scan", follow_redirects=False)
    flag_id = _get_flag_id(app, case_id)

    other_case_id = _create_case(client, label="Other Student")
    response = client.post(f"/cases/{other_case_id}/consistency/flags/{flag_id}/confirm", follow_redirects=False)
    assert response.status_code == 404


def test_flag_route_nonexistent_flag_returns_404(client: TestClient):
    case_id = _create_case(client)
    response = client.post(f"/cases/{case_id}/consistency/flags/99999/confirm", follow_redirects=False)
    assert response.status_code == 404
