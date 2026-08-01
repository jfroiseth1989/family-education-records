"""Tests for the Phase 3.5 Step 4 review UI: GET /cases/{id}/facts, the
promote/reject observation routes, and the create-from-citation route.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AiObservation, VerifiedFact


def _create_case(client: TestClient, label: str = "Review UI Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_and_scan(client: TestClient, case_id: int, text: str = "Held March 12, 2024.") -> int:
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("letter.txt", text.encode(), "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/documents/{document_id}/facts/scan-dates", follow_redirects=False)
    return document_id


# --- review page -------------------------------------------------------


def test_facts_review_page_lists_pending_observation(client: TestClient):
    case_id = _create_case(client)
    _upload_and_scan(client, case_id)

    response = client.get(f"/cases/{case_id}/facts")

    assert response.status_code == 200
    assert "Possible date: 2024-03-12" in response.text
    assert "regex-date-parse-v1" in response.text


def test_facts_review_page_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/facts")
    assert response.status_code == 404


# --- promote -------------------------------------------------------------


def test_promote_observation_creates_verified_fact(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_and_scan(client, case_id)

    with app.state.session_factory() as db:
        observation = db.scalars(select(AiObservation)).one()
        observation_id = observation.observation_id

    response = client.post(
        f"/cases/{case_id}/facts/observations/{observation_id}/promote",
        data={"confidence_label": "probable", "statement": "IEP meeting held March 12, 2024"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case_id}/facts"

    with app.state.session_factory() as db:
        fact = db.scalars(select(VerifiedFact)).one()
        assert fact.statement == "IEP meeting held March 12, 2024"
        assert fact.confidence_label == "probable"
        assert fact.source_observation_id == observation_id

        reviewed = db.get(AiObservation, observation_id)
        assert reviewed.status == "accepted"


def test_promote_observation_wrong_case_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    other_case_id = _create_case(client, label="Other Case")
    _upload_and_scan(client, case_id)

    with app.state.session_factory() as db:
        observation = db.scalars(select(AiObservation)).one()
        observation_id = observation.observation_id

    response = client.post(
        f"/cases/{other_case_id}/facts/observations/{observation_id}/promote",
        data={"confidence_label": "certain"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_promote_observation_invalid_confidence_label_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_and_scan(client, case_id)

    with app.state.session_factory() as db:
        observation_id = db.scalars(select(AiObservation)).one().observation_id

    response = client.post(
        f"/cases/{case_id}/facts/observations/{observation_id}/promote",
        data={"confidence_label": "very sure"},
        follow_redirects=False,
    )
    assert response.status_code == 400


# --- reject ---------------------------------------------------------------


def test_reject_observation_marks_rejected_and_creates_no_fact(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_and_scan(client, case_id)

    with app.state.session_factory() as db:
        observation_id = db.scalars(select(AiObservation)).one().observation_id

    response = client.post(
        f"/cases/{case_id}/facts/observations/{observation_id}/reject",
        data={"reason": "Not relevant"},
        follow_redirects=False,
    )

    assert response.status_code == 303

    with app.state.session_factory() as db:
        reviewed = db.get(AiObservation, observation_id)
        assert reviewed.status == "rejected"
        facts = db.scalars(select(VerifiedFact)).all()
        assert facts == []


def test_reject_observation_already_reviewed_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    _upload_and_scan(client, case_id)

    with app.state.session_factory() as db:
        observation_id = db.scalars(select(AiObservation)).one().observation_id

    client.post(f"/cases/{case_id}/facts/observations/{observation_id}/reject", data={}, follow_redirects=False)
    response = client.post(f"/cases/{case_id}/facts/observations/{observation_id}/reject", data={}, follow_redirects=False)
    assert response.status_code == 400


# --- create-from-citation --------------------------------------------------


def test_create_fact_from_citation(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("letter.txt", b"Some page content to highlight for a fact.", "text/plain")},
        follow_redirects=False,
    )
    document_id = int(response.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        from app.db.models import DocumentPage

        page = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one()
        page_id = page.page_id

    highlight_response = client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 4},
        follow_redirects=False,
    )
    assert highlight_response.status_code == 303

    with app.state.session_factory() as db:
        from app.db.models import Citation

        citation = db.scalars(select(Citation)).one()
        citation_id = citation.citation_id

    fact_response = client.post(
        f"/documents/{document_id}/facts/create-from-citation",
        data={
            "citation_id": citation_id,
            "fact_type": "category",
            "statement": "This document mentions something notable.",
            "confidence_label": "certain",
            "page": 1,
        },
        follow_redirects=False,
    )

    assert fact_response.status_code == 303
    assert fact_response.headers["location"] == f"/documents/{document_id}/view?page=1"

    with app.state.session_factory() as db:
        fact = db.scalars(select(VerifiedFact)).one()
        assert fact.statement == "This document mentions something notable."
        assert fact.source_observation_id is None


def test_create_fact_from_citation_wrong_document_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    response_a = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("a.txt", b"Content for document A here.", "text/plain")},
        follow_redirects=False,
    )
    document_a_id = int(response_a.headers["location"].rsplit("/", 1)[-1])
    response_b = client.post(
        f"/cases/{case_id}/documents",
        files={"file": ("b.txt", b"Content for document B here.", "text/plain")},
        follow_redirects=False,
    )
    document_b_id = int(response_b.headers["location"].rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        from app.db.models import DocumentPage

        page_a = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_a_id)).one()
        page_a_id = page_a.page_id

    client.post(
        f"/documents/{document_a_id}/annotations/highlight",
        data={"page_id": page_a_id, "start_offset": 0, "end_offset": 4},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        from app.db.models import Citation

        citation_id = db.scalars(select(Citation)).one().citation_id

    response = client.post(
        f"/documents/{document_b_id}/facts/create-from-citation",
        data={
            "citation_id": citation_id,
            "fact_type": "category",
            "statement": "Cross-document mismatch.",
            "confidence_label": "certain",
            "page": 1,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
