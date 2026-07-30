"""Seed data — default lookup table rows for a freshly initialized vault."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DocumentType

# Matches the default list in docs/DATA_MODEL.md "documents". Users are not
# limited to this list — DocumentType is a lookup table specifically so
# more rows can be added later without a migration (see
# docs/DATA_MODEL.md "Extensibility"); Phase 1 just doesn't expose a UI for
# adding one yet.
DEFAULT_DOCUMENT_TYPES: list[tuple[str, str]] = [
    ("IEP", "Individualized Education Program"),
    ("504 Plan", "Section 504 accommodation plan"),
    ("Evaluation", "Educational, psychological, or related evaluation report"),
    ("Correspondence", "Letters, emails, or other written communication"),
    ("Discipline", "Disciplinary records or notices"),
    ("Attendance", "Attendance records"),
    ("Grades", "Report cards, transcripts, or grade reports"),
    ("Medical", "Medical or health-related records"),
    ("Legal Filing", "Legal filings, due process documents, or related correspondence"),
    ("Audio Transcript", "Transcript of an audio or video recording"),
    ("Other", "Anything that doesn't fit another category"),
]


def seed_document_types(db: Session) -> None:
    """Insert the default document types if they don't already exist.

    Idempotent by design — safe to call on every application startup
    rather than needing a separate "have we seeded yet" flag.
    """
    existing_names = set(db.scalars(select(DocumentType.name)))
    for name, description in DEFAULT_DOCUMENT_TYPES:
        if name not in existing_names:
            db.add(DocumentType(name=name, description=description))
    db.commit()
