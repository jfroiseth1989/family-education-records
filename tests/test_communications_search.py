"""Tests for app/core/indexing/communications_search.py (Communications
Phase Step 6): free-text FTS5 search over subject/body, structured
filters over plain SQL fields, FTS query safety, and independence from
Document search.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.communications.ingestion import import_eml_file
from app.core.indexing.communications_search import search_communications
from app.core.indexing.search import search_case_documents
from app.core.ingestion.service import ingest_document
from app.db.models import Case, Communication


def _eml(
    *,
    message_id: str,
    subject: str = "IEP Meeting Notice",
    from_addr: str = "amanda.wagner@district.example.org",
    from_name: str = "Amanda Wagner",
    to_addr: str = "parent@yahoo.com",
    cc_addr: str | None = None,
    date: str = "Mon, 7 Mar 2022 14:30:00 -0500",
    body: str = "Please see the attached notice about the upcoming meeting.",
    attachment: bool = False,
) -> bytes:
    cc_line = f"Cc: {cc_addr}\n" if cc_addr else ""
    if not attachment:
        return (
            f"From: {from_name} <{from_addr}>\n"
            f"To: {to_addr}\n"
            f"{cc_line}"
            f"Subject: {subject}\n"
            f"Date: {date}\n"
            f"Message-ID: {message_id}\n"
            f"Content-Type: text/plain; charset=utf-8\n"
            f"\n"
            f"{body}\n"
        ).encode("utf-8")
    return (
        f"From: {from_name} <{from_addr}>\n"
        f"To: {to_addr}\n"
        f"{cc_line}"
        f"Subject: {subject}\n"
        f"Date: {date}\n"
        f"Message-ID: {message_id}\n"
        'Content-Type: multipart/mixed; boundary="BOUNDARY"\n'
        "\n"
        "--BOUNDARY\n"
        "Content-Type: text/plain\n\n"
        f"{body}\n"
        "--BOUNDARY\n"
        "Content-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="notice.pdf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        "JVBERi0xLjQK\n"
        "--BOUNDARY--\n"
    ).encode("utf-8")


def _write(tmp_path: Path, raw: bytes, name: str) -> Path:
    path = tmp_path / "source-files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _import(db: Session, vault, case: Case, tmp_path: Path, raw: bytes, name: str) -> Communication:
    source = _write(tmp_path, raw, name)
    communication = import_eml_file(
        db, vault, case, source_file_path=source, original_filename=name, actor="test-user"
    )
    db.commit()
    return communication


# --- free-text FTS matches ---------------------------------------------


def test_subject_match(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<a@x>", subject="Annual IEP Review"), "a.eml")

    results = search_communications(db_session, query="Annual IEP Review")

    assert len(results) == 1
    assert results[0].subject == "Annual IEP Review"


def test_body_text_match(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<b@x>", body="The transportation plan changes take effect Monday."),
        "b.eml",
    )

    results = search_communications(db_session, query="transportation plan changes")

    assert len(results) == 1


def test_no_match_returns_empty(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<c@x>"), "c.eml")

    results = search_communications(db_session, query="completely unrelated aardvark topic")

    assert results == []


def test_no_query_and_no_filters_returns_empty_without_browsing_everything(
    db_session: Session, vault, sample_case: Case, tmp_path: Path
):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<d@x>"), "d.eml")

    assert search_communications(db_session) == []


# --- structured filters --------------------------------------------------


def test_sender_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<e1@x>", from_addr="teacher@district.example.org", from_name="Teacher One"),
        "e1.eml",
    )
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<e2@x>", from_addr="nurse@district.example.org", from_name="School Nurse"),
        "e2.eml",
    )

    results = search_communications(db_session, sender="nurse")

    assert len(results) == 1
    assert results[0].from_address == "nurse@district.example.org"


def test_recipient_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<f1@x>", to_addr="parent-a@yahoo.com"), "f1.eml",
    )
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<f2@x>", to_addr="parent-b@yahoo.com"), "f2.eml",
    )

    results = search_communications(db_session, recipient="parent-b")

    assert len(results) == 1
    assert results[0].communication_id is not None


def test_cc_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<g1@x>", cc_addr="counselor@district.example.org"), "g1.eml",
    )
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<g2@x>"), "g2.eml",
    )

    results = search_communications(db_session, cc="counselor")

    assert len(results) == 1


def test_subject_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<h1@x>", subject="Field Trip Permission"), "h1.eml")
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<h2@x>", subject="Health Screening"), "h2.eml")

    results = search_communications(db_session, subject="Field Trip")

    assert len(results) == 1
    assert results[0].subject == "Field Trip Permission"


def test_date_range_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<i1@x>", date="Mon, 7 Mar 2022 14:30:00 -0500"), "i1.eml",
    )
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<i2@x>", date="Fri, 1 Jul 2022 09:00:00 -0500"), "i2.eml",
    )

    results = search_communications(db_session, date_from=date(2022, 3, 1), date_to=date(2022, 3, 31))

    assert len(results) == 1
    assert results[0].communication_id is not None


def test_date_range_end_is_inclusive_through_end_of_day(
    db_session: Session, vault, sample_case: Case, tmp_path: Path
):
    # Sent late in the day on the last day of the window -- must still match.
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<j1@x>", date="Thu, 31 Mar 2022 23:30:00 -0500"), "j1.eml",
    )

    results = search_communications(db_session, date_from=date(2022, 3, 1), date_to=date(2022, 3, 31))

    assert len(results) == 1


def test_has_attachments_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<k1@x>", attachment=True), "k1.eml")
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<k2@x>", attachment=False), "k2.eml")

    with_attachments = search_communications(db_session, has_attachments=True)
    without_attachments = search_communications(db_session, has_attachments=False)

    assert len(with_attachments) == 1
    assert with_attachments[0].has_attachments is True
    assert len(without_attachments) == 1
    assert without_attachments[0].has_attachments is False


def test_case_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    other_case = Case(label="Other Student")
    db_session.add(other_case)
    db_session.commit()

    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<l1@x>"), "l1.eml")
    _import(db_session, vault, other_case, tmp_path, _eml(message_id="<l2@x>"), "l2.eml")

    results = search_communications(db_session, case_id=sample_case.case_id)

    assert len(results) == 1
    assert results[0].case_id == sample_case.case_id


def test_combined_fts_and_structured_filters(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<m1@x>", subject="IEP Meeting", from_addr="teacher@district.example.org"),
        "m1.eml",
    )
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<m2@x>", subject="IEP Meeting", from_addr="nurse@district.example.org"),
        "m2.eml",
    )

    results = search_communications(db_session, query="IEP Meeting", sender="teacher")

    assert len(results) == 1
    assert results[0].from_address == "teacher@district.example.org"


# --- thread filter ---------------------------------------------------------


def test_threaded_filter(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    parent_raw = (
        "From: teacher@district.example.org\n"
        "To: parent@yahoo.com\n"
        "Subject: Thread Topic\n"
        "Date: Mon, 7 Mar 2022 09:00:00 -0500\n"
        "Message-ID: <thr-p@x>\n\n"
        "Starting the thread.\n"
    ).encode()
    reply_raw = (
        "From: parent@yahoo.com\n"
        "To: teacher@district.example.org\n"
        "Subject: Re: Thread Topic\n"
        "Date: Tue, 8 Mar 2022 09:00:00 -0500\n"
        "Message-ID: <thr-r@x>\n"
        "In-Reply-To: <thr-p@x>\n\n"
        "Replying.\n"
    ).encode()
    solo_raw = _eml(message_id="<thr-solo@x>", subject="Unrelated")

    _import(db_session, vault, sample_case, tmp_path, parent_raw, "p.eml")
    _import(db_session, vault, sample_case, tmp_path, reply_raw, "r.eml")
    _import(db_session, vault, sample_case, tmp_path, solo_raw, "solo.eml")

    threaded_results = search_communications(db_session, threaded=True)
    unthreaded_results = search_communications(db_session, threaded=False)

    assert len(threaded_results) == 2
    assert all(r.thread_id is not None for r in threaded_results)
    assert len(unthreaded_results) == 1
    assert unthreaded_results[0].thread_id is None


# --- soft-delete exclusion, live indexing --------------------------------


def test_soft_deleted_communication_excluded(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    communication = _import(
        db_session, vault, sample_case, tmp_path, _eml(message_id="<n1@x>", subject="Deleted Topic"), "n1.eml"
    )

    assert len(search_communications(db_session, query="Deleted Topic")) == 1

    communication.deleted_at = communication.imported_at
    db_session.commit()

    assert search_communications(db_session, query="Deleted Topic") == []


def test_newly_imported_communication_is_immediately_searchable(
    db_session: Session, vault, sample_case: Case, tmp_path: Path
):
    assert search_communications(db_session, query="Freshly Imported Topic") == []

    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<o1@x>", subject="Freshly Imported Topic"), "o1.eml")

    assert len(search_communications(db_session, query="Freshly Imported Topic")) == 1


def test_result_disappears_when_soft_deleted_after_indexing(
    db_session: Session, vault, sample_case: Case, tmp_path: Path
):
    communication = _import(
        db_session, vault, sample_case, tmp_path, _eml(message_id="<p1@x>", subject="Vanishing Topic"), "p1.eml"
    )
    assert len(search_communications(db_session, query="Vanishing Topic")) == 1

    db_session.execute(
        text("UPDATE communications SET deleted_at = imported_at WHERE communication_id = :id"),
        {"id": communication.communication_id},
    )
    db_session.commit()

    assert search_communications(db_session, query="Vanishing Topic") == []


# --- FTS query safety -------------------------------------------------


def test_malformed_fts_special_characters_do_not_crash(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<q1@x>"), "q1.eml")

    dangerous_inputs = [
        '"unterminated quote',
        "AND OR NOT",
        "subject:hack",
        "*",
        "()",
        "NEAR(a b)",
        "a\"b\"c\"",
        "-- sql comment",
        "'; DROP TABLE communications; --",
    ]
    for dangerous in dangerous_inputs:
        results = search_communications(db_session, query=dangerous)  # must not raise
        assert isinstance(results, list)


def test_unicode_and_punctuation_query(db_session: Session, vault, sample_case: Case, tmp_path: Path):
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<r1@x>", subject="Café Meeting — Résumé Review", body="Discussion of José's IEP."),
        "r1.eml",
    )

    results = search_communications(db_session, query="Café Meeting")
    assert len(results) == 1

    results2 = search_communications(db_session, query="José's IEP")
    assert len(results2) == 1


# --- independence from Document search -----------------------------------


def test_communications_search_does_not_affect_document_search(
    db_session: Session, vault, sample_case: Case, tmp_path: Path
):
    doc_source = tmp_path / "source-files" / "doc.txt"
    doc_source.parent.mkdir(parents=True, exist_ok=True)
    doc_source.write_text("This is a Prior Written Notice about placement changes.")
    from app.core.extraction.service import extract_document

    document = ingest_document(
        db_session, vault, sample_case, source_file_path=doc_source,
        original_filename="doc.txt", actor="test-user",
    )
    db_session.commit()
    extract_document(db_session, vault, document, "test-user")
    db_session.commit()

    doc_results_before = search_case_documents(db_session, sample_case.case_id, "Prior Written Notice")
    assert len(doc_results_before) == 1

    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<s1@x>", subject="Prior Written Notice", body="Prior Written Notice email content."),
        "s1.eml",
    )

    doc_results_after = search_case_documents(db_session, sample_case.case_id, "Prior Written Notice")
    assert len(doc_results_after) == 1
    assert doc_results_after[0].document_id == doc_results_before[0].document_id

    comm_results = search_communications(db_session, query="Prior Written Notice")
    assert len(comm_results) == 1
