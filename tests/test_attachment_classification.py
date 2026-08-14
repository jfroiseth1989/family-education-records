"""Tests for app/core/communications/attachment_classification.py
(Communications Phase Step 5).

Covers the classifier itself (a thin wrapper reusing
app/core/document_type_suggestion.py) and the new Phase-1-DocumentType
trigger coverage added to that module this step, including the
Reevaluation/Eligibility Determination -> Evaluation synonym rule
(docs/COMMUNICATIONS_PLAN.md §1 decision 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.communications.attachment_classification import classify_attachment, resolve_document_type_id
from app.core.document_type_suggestion import suggest_document_type
from app.db.models import DocumentType


# --- suggest_document_type: new Phase 1 trigger coverage -------------------


def test_evaluation_report_filename_suggests_evaluation():
    suggestion = suggest_document_type("2024 Psychoeducational Evaluation.pdf")
    assert suggestion is not None
    assert suggestion.type_name == "Evaluation"


def test_reevaluation_suggests_evaluation():
    suggestion = suggest_document_type("Reevaluation Report.pdf")
    assert suggestion is not None
    assert suggestion.type_name == "Evaluation"


def test_hyphenated_re_evaluation_suggests_evaluation():
    suggestion = suggest_document_type("Re-Evaluation Notice.pdf")
    assert suggestion is not None
    assert suggestion.type_name == "Evaluation"


def test_eligibility_determination_suggests_evaluation():
    suggestion = suggest_document_type("Eligibility Determination.pdf")
    assert suggestion is not None
    assert suggestion.type_name == "Evaluation"


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("Section 504 Plan.pdf", "504 Plan"),
        ("Correspondence Log.pdf", "Correspondence"),
        ("Discipline Referral.pdf", "Discipline"),
        ("Attendance Record.pdf", "Attendance"),
        ("Grade Report Q1.pdf", "Grades"),
        ("Medical Record.pdf", "Medical"),
        ("Legal Filing - Notice.pdf", "Legal Filing"),
        ("Audio Transcript - March Team Meeting.pdf", "Audio Transcript"),
    ],
)
def test_phase1_document_type_trigger_coverage(filename: str, expected_type: str):
    suggestion = suggest_document_type(filename)
    assert suggestion is not None, f"expected a suggestion for {filename!r}"
    assert suggestion.type_name == expected_type


def test_ambiguous_filename_yields_no_suggestion():
    # Contains both an IEP-style trigger and an evaluation trigger with no
    # single dominant match -- see document_type_suggestion.py's own
    # "several categories match, show no suggestion" rule.
    suggestion = suggest_document_type("IEP and Evaluation Report.pdf")
    assert suggestion is None


def test_unrelated_filename_yields_no_suggestion():
    suggestion = suggest_document_type("family_photo.jpg")
    assert suggestion is None


# --- classify_attachment: file-reading behavior -----------------------------


def test_classify_attachment_from_pdf_text(tmp_path: Path):
    # No extractor is registered for .txt in a way that matters here --
    # what matters is that a supported extension's *content* can also
    # drive the suggestion, not just the filename. Use .txt, which does
    # have a native extractor (app/core/extraction/text.py).
    path = tmp_path / "notice.txt"
    path.write_text("This is a Prior Written Notice regarding placement.", encoding="utf-8")

    suggestion = classify_attachment("notice.txt", path)

    assert suggestion is not None
    assert suggestion.type_name == "Prior Written Notice"


def test_classify_attachment_filename_only_when_unsupported_extension(tmp_path: Path):
    path = tmp_path / "iep-scan.bin"
    path.write_bytes(b"\x00\x01\x02\x03 not a real format")

    suggestion = classify_attachment("iep-scan.bin", path)

    # No extractor for .bin -- falls back to filename-only matching,
    # which still finds "IEP" in the name. Proves an unsupported format
    # doesn't crash classification, just narrows its evidence.
    assert suggestion is not None
    assert suggestion.type_name == "IEP"


def test_classify_attachment_unreadable_file_fails_safely(tmp_path: Path):
    # A .pdf extension routes to the PDF extractor, but this isn't a real
    # PDF -- extraction should fail internally and be swallowed, not
    # raised, per classify_attachment's "fail safely" contract.
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a valid pdf file at all")

    suggestion = classify_attachment("broken.pdf", path)  # must not raise

    assert suggestion is None


def test_classify_attachment_missing_file_fails_safely(tmp_path: Path):
    missing = tmp_path / "does-not-exist.pdf"

    suggestion = classify_attachment("does-not-exist.pdf", missing)  # must not raise

    assert suggestion is None


def test_classify_attachment_no_match_returns_none(tmp_path: Path):
    path = tmp_path / "random-file.txt"
    path.write_text("Just an ordinary note about the weekend.", encoding="utf-8")

    suggestion = classify_attachment("random-file.txt", path)

    assert suggestion is None


# --- resolve_document_type_id -----------------------------------------------


def test_resolve_document_type_id_resolves_seeded_type(db_session: Session):
    suggestion = suggest_document_type("IEP.pdf")
    type_id = resolve_document_type_id(db_session, suggestion)

    expected = db_session.query(DocumentType).filter_by(name="IEP").one()
    assert type_id == expected.type_id


def test_resolve_document_type_id_none_when_no_suggestion(db_session: Session):
    assert resolve_document_type_id(db_session, None) is None
