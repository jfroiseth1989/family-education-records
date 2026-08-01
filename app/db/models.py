"""SQLAlchemy ORM models for the Phase 1 schema.

Scope note: this Phase 1 schema implements only the tables needed for case
management, document ingestion, version tracking, and chain of custody —
see docs/PROJECT_PLAN.md "Phase 1 — Foundations". Tables for later phases
(extraction, OCR, facts/observations, the relationship graph, annotations,
etc.) are intentionally not created yet. docs/DATA_MODEL.md is the
authoritative full target schema; each later phase's migration builds
toward it incrementally, which is exactly what the lookup-table /
EAV-metadata extensibility design in that document is for — adding a table
or column later is additive, not a redesign.

Every table here traces to a specific requirement discussed and approved
before implementation began:
  - `cases`                    — case management (original Phase 1 scope).
  - `document_types`           — lookup table, not a hardcoded enum, so a
                                  new document type is a row insert later.
  - `document_version_groups`,
    `documents` (version_* columns) — document version tracking: a
                                  corrected/reissued record is a new row
                                  linked to, never overwriting, the old one.
  - `document_custody_events`  — the per-document chain-of-custody ledger.
  - `audit_log`                — case/system-level activity, kept separate
                                  from the document-specific custody ledger.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CaseStatus(str, enum.Enum):
    """Allowed values for Case.status.

    Deliberately a small fixed enum, not a lookup table — unlike document
    types or event types, a case's lifecycle state is a structural concept
    of this application, not a user-extensible category. See
    docs/DATA_MODEL.md "Extensibility" for the distinction.
    """

    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class DocumentDateSource(str, enum.Enum):
    """How `Document.document_date` was determined.

    `document_date` is the date *on* the record itself (e.g. the date
    printed on a letter, the date an evaluation was conducted) — never
    derived from filesystem metadata or `Document.ingested_at`, which only
    reflect when this app happened to see the file. See
    docs/PHASE_1_REVIEW.md item 4 and app/core/document_dates.py.

    Only MANUAL is set anywhere in Phase 1. EXTRACTED and FILE_METADATA are
    reserved for later phases (automated date suggestion from a document's
    text, or from a source like an email's Date header) so that adding
    those sources later is a matter of setting this column to a different
    value, not a schema change.
    """

    MANUAL = "manual"
    EXTRACTED = "extracted"
    FILE_METADATA = "file_metadata"


class DocumentDatePrecision(str, enum.Enum):
    """How precisely `Document.document_date` is known.

    RANGE represents a genuine date span (e.g. "sometime in March 2024," a
    triennial evaluation window) rather than a single day — special
    education records routinely carry dates like this. When precision is
    RANGE, `Document.document_date` holds the range's start and
    `Document.document_date_range_end` holds its end; for EXACT or
    APPROXIMATE, `document_date_range_end` is always null. This is a
    reusable pattern — see docs/DATA_MODEL.md "Date representation
    pattern" — intended for any future date-bearing table (e.g.
    `timeline_events` in Phase 4), not something reinvented per table.
    """

    EXACT = "exact"
    APPROXIMATE = "approximate"
    RANGE = "range"


class Case(Base):
    """A single matter/dispute — the top-level container for its documents."""

    __tablename__ = "cases"

    case_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CaseStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    version_groups: Mapped[list["DocumentVersionGroup"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class DocumentType(Base):
    """Lookup table for document categories (IEP, evaluation, ...).

    A lookup table rather than a hardcoded enum/column so a new document
    type can be added later with an INSERT, not a migration — see
    docs/DATA_MODEL.md "Extensibility". Seeded with defaults by
    app/db/seed.py.
    """

    __tablename__ = "document_types"

    type_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="document_type")


class DocumentVersionGroup(Base):
    """The stable identity for a "logical record" across its versions.

    e.g. "IEP — Jane Doe" spans however many corrected/reissued files
    arrive over time. See docs/ARCHITECTURE.md §3.2 and
    docs/DATA_MODEL.md "document_version_groups".

    Implementation note: `current_document_id` intentionally has no
    database-level foreign key. Declaring one would create a circular
    table dependency with `Document.version_group_id` (this table
    references documents, and documents references this table), and
    SQLite cannot add a foreign key constraint after both tables exist
    (it has no `ALTER TABLE ... ADD CONSTRAINT`), which is the usual way
    SQLAlchemy breaks such cycles on other databases via `use_alter=True`.
    The pointer is instead kept correct by application logic — see
    `app/core/ingestion/versioning.py` — in the same transaction as every
    write that could change it, and is covered by tests.
    """

    __tablename__ = "document_version_groups"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # No ForeignKey here — see class docstring.
    current_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    case: Mapped["Case"] = relationship(back_populates="version_groups")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="version_group",
        foreign_keys="Document.version_group_id",
    )
    current_document: Mapped["Document | None"] = relationship(
        "Document",
        primaryjoin="DocumentVersionGroup.current_document_id == Document.document_id",
        foreign_keys=[current_document_id],
        viewonly=True,
    )


class Document(Base):
    """One ingested file. Immutable once created — see module docstring."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("case_id", "sha256_hash", name="uq_document_case_hash"),
        # Enforces "exactly one current version per version group" at the
        # database level, not just by application discipline: SQLite
        # partial unique indexes treat every NULL version_group_id as
        # distinct, so ungrouped documents (the common case — most
        # documents never get versioned) are unaffected.
        Index(
            "uq_one_current_version_per_group",
            "version_group_id",
            unique=True,
            sqlite_where=text("is_current_version = 1"),
        ),
    )

    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # Path relative to the vault root — never an absolute path, so the
    # vault can be moved/backed up without invalidating stored references.
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    source: Mapped[str | None] = mapped_column(String(300), nullable=True)
    document_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_types.type_id"), nullable=True
    )

    document_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Only meaningful when document_date_precision == "range"; null
    # otherwise. See DocumentDatePrecision.RANGE docstring.
    document_date_range_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    document_date_precision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    document_date_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ingested_by: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Versioning (docs/ARCHITECTURE.md §3.2) ---
    version_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_version_groups.group_id"), nullable=True
    )
    version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_current_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supersedes_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.document_id"), nullable=True
    )
    version_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    case: Mapped["Case"] = relationship(back_populates="documents")
    document_type: Mapped["DocumentType | None"] = relationship(back_populates="documents")
    version_group: Mapped["DocumentVersionGroup | None"] = relationship(
        back_populates="documents", foreign_keys=[version_group_id]
    )
    supersedes: Mapped["Document | None"] = relationship(
        remote_side=[document_id], foreign_keys=[supersedes_document_id]
    )
    custody_events: Mapped[list["DocumentCustodyEvent"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentCustodyEvent.event_timestamp",
    )


class DocumentCustodyEvent(Base):
    """Append-only chain-of-custody ledger entry for one document.

    See docs/DATA_MODEL.md "document_custody_events". Nothing in this
    application exposes an update or delete path for this table —
    app/core/custody.py only ever inserts new rows.
    """

    __tablename__ = "document_custody_events"

    custody_event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), nullable=False
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)

    # Snapshot fields, recorded at *this* event, not just once at import —
    # see docs/DATA_MODEL.md "document_custody_events" for why.
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256_hash_at_event: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes_at_event: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_location_at_event: Mapped[str] = mapped_column(String(1000), nullable=False)

    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="custody_events")


class AuditLog(Base):
    """Append-only case/system-level activity log.

    Document-specific activity lives in DocumentCustodyEvent instead — see
    docs/DATA_MODEL.md "audit_log". This table covers case-level actions
    (e.g. case created/edited) for Phase 1.
    """

    __tablename__ = "audit_log"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.case_id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
