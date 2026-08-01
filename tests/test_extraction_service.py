"""Tests for app/core/extraction/service.py -- extraction orchestration.

Covers the four §12 integrity requirements directly:
  - 12.1: extraction status is a first-class, queryable field.
  - 12.2: every page links back to the source document's hash.
  - 12.3: extraction never modifies the stored original (the most
    important regression test in this file).
  - 12.4: citations exist as a table but nothing here writes to it yet.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import fitz
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.extraction.service import extract_document
from app.core.files import compute_sha256
from app.core.ingestion.service import ingest_document
from app.core.vault import VaultLayout
from app.db.models import Case, Citation, Document, DocumentCustodyEvent, DocumentPage


def _make_pdf(path: Path, text: str = "Hello World from a test PDF.") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _make_scanned_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 20, 20))
    pixmap.set_rect(pixmap.irect, (200, 0, 0))
    page.insert_image(fitz.Rect(72, 72, 200, 200), pixmap=pixmap)
    doc.save(str(path))
    doc.close()


def _ingest(db, vault, case, path: Path, filename: str) -> Document:
    document = ingest_document(db, vault, case, path, filename, actor="test-user")
    db.commit()
    return document


# --- Core success path + the two most important integrity guarantees -----


def test_extract_document_populates_pages_and_status(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert document.extraction_status == "completed"
    assert document.extraction_error is None
    assert document.page_count == 1
    assert document.has_text_layer is True
    assert document.needs_ocr is False

    pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).all()
    assert len(pages) == 1
    assert "Hello World" in pages[0].extracted_text


def test_extraction_never_modifies_the_stored_original(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """The single most important regression test in this file -- §12.3."""
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    stored_path = vault.root / document.stored_path
    hash_before = compute_sha256(stored_path)
    content_before = stored_path.read_bytes()

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    hash_after = compute_sha256(stored_path)
    assert hash_after == hash_before
    assert stored_path.read_bytes() == content_before
    assert document.sha256_hash == hash_before  # recorded hash also unchanged


def test_document_page_source_sha256_matches_document_hash(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    """§12.2: every page links back to the source document's hash."""
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).all()
    assert len(pages) == 1
    assert pages[0].source_sha256 == document.sha256_hash


def test_extract_document_writes_extracted_custody_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    events = db_session.scalars(
        select(DocumentCustodyEvent).where(
            DocumentCustodyEvent.document_id == document.document_id,
            DocumentCustodyEvent.event_type == "extracted",
        )
    ).all()
    assert len(events) == 1
    assert events[0].details["page_count"] == 1


# --- needs-OCR heuristic, integration-level -------------------------------


def test_scanned_pdf_page_flagged_needs_ocr(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "scanned.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert document.needs_ocr is True
    assert document.extraction_status == "completed"  # extraction ran fine; OCR is separate
    page = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).one()
    assert page.needs_ocr is True


# --- images: no extractor call, flagged directly --------------------------


def test_image_file_flagged_needs_ocr_without_extraction_attempt(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    image_path = tmp_path / "scan.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes for testing")
    document = _ingest(db_session, vault, sample_case, image_path, "scan.jpg")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert document.extraction_status == "completed"
    assert document.needs_ocr is True
    assert document.has_text_layer is False
    page = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).one()
    assert page.extraction_method == "none"
    assert page.extracted_text is None


# --- unsupported format -----------------------------------------------


def test_unsupported_format_ingests_successfully_with_no_pages(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b,c\n1,2,3\n")
    document = _ingest(db_session, vault, sample_case, csv_path, "data.csv")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert document.extraction_status == "unsupported_format"
    assert document.extraction_error is None
    pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).all()
    assert pages == []


# --- extraction failure never propagates (approved decision 6) -----------


def test_corrupt_pdf_ingestion_succeeds_but_extraction_is_flagged_failed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    corrupt_path = tmp_path / "corrupt.pdf"
    corrupt_path.write_bytes(b"not a valid pdf at all, just garbage bytes")

    # Ingestion itself must succeed regardless of what happens during extraction.
    document = _ingest(db_session, vault, sample_case, corrupt_path, "corrupt.pdf")
    assert document.document_id is not None

    # extract_document() must not raise.
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    assert document.extraction_status == "failed"
    assert document.extraction_error  # non-empty, descriptive message

    events = db_session.scalars(
        select(DocumentCustodyEvent).where(
            DocumentCustodyEvent.document_id == document.document_id,
            DocumentCustodyEvent.event_type == "extraction_failed",
        )
    ).all()
    assert len(events) == 1


# --- idempotent re-extraction --------------------------------------------


def test_reextraction_replaces_pages_and_logs_re_extracted_event(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, "First extraction text")
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    first_pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).all()
    assert len(first_pages) == 1

    # Re-run extraction against the same (unchanged) stored file. Re-running
    # twice in a row further confirms rows are replaced, not accumulated.
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()
    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    second_pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).all()
    assert len(second_pages) == 1  # replaced each time, never accumulated

    event_types = [e.event_type for e in document.custody_events]
    assert event_types == ["imported", "extracted", "re_extracted", "re_extracted"]


# --- email attachments -----------------------------------------------


def test_email_attachment_is_ingested_and_extracted_as_child_document(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "recipient@example.com"
    message["Subject"] = "Email with attachment"
    message.set_content("See attached.")
    message.add_attachment(
        b"This is the attachment's text content, over ten words long for testing purposes.",
        maintype="text",
        subtype="plain",
        filename="attachment.txt",
    )
    eml_path = tmp_path / "with_attachment.eml"
    eml_path.write_bytes(bytes(message))

    parent = _ingest(db_session, vault, sample_case, eml_path, "with_attachment.eml")
    extract_document(db_session, vault, parent, actor="test-user")
    db_session.commit()

    all_case_documents = db_session.scalars(
        select(Document).where(Document.case_id == sample_case.case_id)
    ).all()
    assert len(all_case_documents) == 2  # parent email + attachment

    child = next(d for d in all_case_documents if d.document_id != parent.document_id)
    assert child.original_filename == "attachment.txt"
    assert f"document #{parent.document_id}" in (child.source or "")
    assert child.extraction_status == "completed"

    child_pages = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == child.document_id)
    ).all()
    assert len(child_pages) == 1
    assert "attachment's text content" in child_pages[0].extracted_text


def test_email_without_attachments_creates_no_extra_documents(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["Subject"] = "No attachments here"
    message.set_content("Just a plain message.")
    eml_path = tmp_path / "plain.eml"
    eml_path.write_bytes(bytes(message))

    parent = _ingest(db_session, vault, sample_case, eml_path, "plain.eml")
    extract_document(db_session, vault, parent, actor="test-user")
    db_session.commit()

    all_case_documents = db_session.scalars(
        select(Document).where(Document.case_id == sample_case.case_id)
    ).all()
    assert len(all_case_documents) == 1


# --- citations table exists but is untouched by Step 1 (§12.4) -----------


def test_citations_table_exists_and_is_not_written_by_extraction(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    document = _ingest(db_session, vault, sample_case, pdf_path, "doc.pdf")

    extract_document(db_session, vault, document, actor="test-user")
    db_session.commit()

    citations = db_session.scalars(select(Citation)).all()
    assert citations == []

    # The table is fully usable, though -- nothing about its schema is
    # aspirational. This is exactly the shape Step 4's highlight creation
    # will use.
    page = db_session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document.document_id)
    ).one()
    citation = Citation(
        document_id=document.document_id,
        page_id=page.page_id,
        start_offset=0,
        end_offset=5,
        quoted_text="Hello",
    )
    db_session.add(citation)
    db_session.commit()
    assert db_session.scalars(select(Citation)).one().quoted_text == "Hello"
