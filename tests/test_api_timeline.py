"""Tests for the Phase 4 Step 3 timeline UI: GET/POST /cases/{id}/timeline,
attach-fact, and delete routes.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Citation, Document, DocumentPage, TimelineEvent, VerifiedFact


def _create_case(client: TestClient, label: str = "Timeline Test Case") -> int:
    response = client.post("/cases", data={"label": label}, follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _upload_document(client: TestClient, case_id: int, filename: str, text: bytes) -> int:
    # Embed the filename into the content so byte-identical-looking calls
    # never collide with ingestion's duplicate-content-within-a-case guard.
    unique_content = text + b" " + filename.encode()
    response = client.post(
        f"/cases/{case_id}/documents",
        files={"file": (filename, unique_content, "text/plain")},
        follow_redirects=False,
    )
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _create_date_fact(client: TestClient, app: FastAPI, case_id: int, fact_date: str, statement: str = "IEP meeting held") -> int:
    document_id = _upload_document(client, case_id, f"doc-{fact_date}.txt", b"Some page content for a fact.")

    with app.state.session_factory() as db:
        page_id = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one().page_id

    client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 4},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        citation_id = db.scalars(
            select(Citation).where(Citation.document_id == document_id)
        ).one().citation_id

    response = client.post(
        f"/documents/{document_id}/facts/create-from-citation",
        data={
            "citation_id": citation_id,
            "fact_type": "date",
            "statement": statement,
            "confidence_label": "certain",
            "fact_date": fact_date,
            "page": 1,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        return db.scalars(select(VerifiedFact).where(VerifiedFact.statement == statement)).one().fact_id


def _create_category_fact(client: TestClient, app: FastAPI, case_id: int, statement: str) -> int:
    document_id = _upload_document(client, case_id, f"doc-{statement[:10]}.txt", b"Some other page content.")

    with app.state.session_factory() as db:
        page_id = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).one().page_id

    client.post(
        f"/documents/{document_id}/annotations/highlight",
        data={"page_id": page_id, "start_offset": 0, "end_offset": 4},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        citation_id = db.scalars(
            select(Citation).where(Citation.document_id == document_id)
        ).one().citation_id

    client.post(
        f"/documents/{document_id}/facts/create-from-citation",
        data={
            "citation_id": citation_id,
            "fact_type": "category",
            "statement": statement,
            "confidence_label": "certain",
            "page": 1,
        },
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        return db.scalars(select(VerifiedFact).where(VerifiedFact.statement == statement)).one().fact_id


# --- view page -------------------------------------------------------------


def test_timeline_page_empty_case(client: TestClient):
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}/timeline")
    assert response.status_code == 200
    assert "No timeline events yet" in response.text
    assert "No verified facts with a date exist yet" in response.text


def test_timeline_page_nonexistent_case_returns_404(client: TestClient):
    response = client.get("/cases/99999/timeline")
    assert response.status_code == 404


# --- create event -------------------------------------------------------


def test_create_timeline_event_succeeds(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")

    response = client.post(
        f"/cases/{case_id}/timeline",
        data={
            "event_type": "meeting",
            "title": "IEP annual review meeting",
            "date_fact_id": date_fact_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case_id}/timeline"

    with app.state.session_factory() as db:
        event = db.scalars(select(TimelineEvent)).one()
        assert event.title == "IEP annual review meeting"
        assert event.event_date_precision == "exact"

        entries = db.scalars(
            select(AuditLog).where(AuditLog.event_type == "timeline_event_created")
        ).all()
        assert len(entries) == 1


def test_create_timeline_event_with_supporting_fact(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    support_fact_id = _create_category_fact(client, app, case_id, "Attendees noted in minutes")

    response = client.post(
        f"/cases/{case_id}/timeline",
        data={
            "event_type": "meeting",
            "title": "IEP annual review meeting",
            "date_fact_id": date_fact_id,
            "additional_fact_ids": [support_fact_id],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/cases/{case_id}/timeline")
    assert "Attendees noted in minutes" in detail.text


def test_create_timeline_event_invalid_event_type_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")

    response = client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "not-a-real-type", "title": "A meeting", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_create_timeline_event_rejects_non_date_fact(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    support_fact_id = _create_category_fact(client, app, case_id, "Not a date fact")

    response = client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting", "date_fact_id": support_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_create_timeline_event_range_precision(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")

    response = client.post(
        f"/cases/{case_id}/timeline",
        data={
            "event_type": "evaluation",
            "title": "Evaluation window",
            "date_fact_id": date_fact_id,
            "event_date_precision": "range",
            "event_date_range_end": "2024-03-20",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as db:
        event = db.scalars(select(TimelineEvent)).one()
        assert event.event_date_precision == "range"
        assert event.event_date_range_end is not None


# --- attach-fact ----------------------------------------------------------


def test_attach_fact_to_event_succeeds(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        event_id = db.scalars(select(TimelineEvent)).one().event_id

    support_fact_id = _create_category_fact(client, app, case_id, "Late-added supporting fact")

    response = client.post(
        f"/cases/{case_id}/timeline/{event_id}/attach-fact",
        data={"fact_id": support_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/cases/{case_id}/timeline")
    assert "Late-added supporting fact" in detail.text


def test_attach_fact_wrong_case_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    other_case_id = _create_case(client, label="Other Case")
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        event_id = db.scalars(select(TimelineEvent)).one().event_id

    response = client.post(
        f"/cases/{other_case_id}/timeline/{event_id}/attach-fact",
        data={"fact_id": date_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_attach_duplicate_fact_returns_400(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        event_id = db.scalars(select(TimelineEvent)).one().event_id

    response = client.post(
        f"/cases/{case_id}/timeline/{event_id}/attach-fact",
        data={"fact_id": date_fact_id},
        follow_redirects=False,
    )
    assert response.status_code == 400


# --- delete -----------------------------------------------------------


def test_delete_timeline_event_soft_deletes_and_hides_from_list(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting to delete", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        event_id = db.scalars(select(TimelineEvent)).one().event_id

    response = client.post(f"/cases/{case_id}/timeline/{event_id}/delete", follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as db:
        event = db.get(TimelineEvent, event_id)
        assert event.deleted_at is not None
        # The underlying fact is never touched.
        fact = db.get(VerifiedFact, date_fact_id)
        assert fact.deleted_at is None

    listing = client.get(f"/cases/{case_id}/timeline")
    assert "A meeting to delete" not in listing.text


def test_delete_timeline_event_wrong_case_returns_404(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    other_case_id = _create_case(client, label="Other Case")
    date_fact_id = _create_date_fact(client, app, case_id, "2024-03-12")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "A meeting", "date_fact_id": date_fact_id},
        follow_redirects=False,
    )
    with app.state.session_factory() as db:
        event_id = db.scalars(select(TimelineEvent)).one().event_id

    response = client.post(f"/cases/{other_case_id}/timeline/{event_id}/delete", follow_redirects=False)
    assert response.status_code == 404


# --- filters and gap display ------------------------------------------


def test_timeline_filters_by_event_type(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    meeting_fact_id = _create_date_fact(client, app, case_id, "2024-03-12", statement="Meeting date fact")
    eval_fact_id = _create_date_fact(client, app, case_id, "2024-04-01", statement="Evaluation date fact")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "Meeting Event", "date_fact_id": meeting_fact_id},
        follow_redirects=False,
    )
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "evaluation", "title": "Evaluation Event", "date_fact_id": eval_fact_id},
        follow_redirects=False,
    )

    with app.state.session_factory() as db:
        from app.db.models import EventType

        meeting_type_id = db.scalars(select(EventType.type_id).where(EventType.name == "meeting")).one()

    response = client.get(f"/cases/{case_id}/timeline?event_type_id={meeting_type_id}")
    assert "Meeting Event" in response.text
    assert "Evaluation Event" not in response.text


def test_timeline_shows_gap_between_events(client: TestClient, app: FastAPI):
    case_id = _create_case(client)
    early_fact_id = _create_date_fact(client, app, case_id, "2024-03-12", statement="Early date fact")
    later_fact_id = _create_date_fact(client, app, case_id, "2024-03-20", statement="Later date fact")
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "Early Event", "date_fact_id": early_fact_id},
        follow_redirects=False,
    )
    client.post(
        f"/cases/{case_id}/timeline",
        data={"event_type": "meeting", "title": "Later Event", "date_fact_id": later_fact_id},
        follow_redirects=False,
    )

    response = client.get(f"/cases/{case_id}/timeline")
    assert "8 days since the previous event" in response.text
