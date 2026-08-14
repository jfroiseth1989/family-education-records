"""SQLAlchemy ORM models.

Scope note: implements the tables built through Phase 2 (case management,
document ingestion, version tracking, chain of custody — Phase 1;
extraction status and the `document_pages`/`citations` traceability
primitives — Step 1; the `document_text_fts` search index — Step 2,
defined in a hand-written migration, not here, see
app/db/migrations/versions/08c778ee32af_*.py; case-scoped tags — Step 3;
`annotation_types`/`annotations` — Step 4; `annotation_notes_fts` — Step 5,
also hand-written, see app/db/migrations/versions/d93ac0658fac_*.py) plus
Phase 3 Steps 0-4 (`ocr_jobs` — job-queue infrastructure, Step 0;
`citations.text_source`/`source_confidence`, `document_pages.
ocr_word_boxes`, and `ocr_text_history` — OCR execution core, Step 1;
`ocr_corrections` — correction layer, Step 3; Steps 2 and 4 added no
schema) plus Phase 3.5 Step 1 (`fact_types`, `ai_observations`,
`ai_observation_citations`, `verified_facts`, `verified_fact_citations`,
`ai_summaries`, `summary_source_documents` — the Fact, Observation &
Summary Layer, see docs/ARCHITECTURE.md §3.7) plus Phase 4 Step 0
(`ai_observations.observed_date`, `verified_facts.fact_date` — structured
dates the timeline reads from, added so Phase 4 never requires a human
to re-enter a date already captured in a verified fact; see
docs/PHASE_4_IMPLEMENTATION_PLAN.md §1/§3) plus Phase 4 Step 1
(`event_types`, `timeline_events`, `timeline_event_facts` — the
timeline schema itself, see docs/ARCHITECTURE.md §3.8 and
docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 1; no core module or UI
exists yet -- those are Steps 2-3). See
docs/PHASE_3_IMPLEMENTATION_PLAN.md for the full Phase 3 schema and step
breakdown. Tables for later phases (relationship graph, etc.) are
intentionally not created yet.
docs/DATA_MODEL.md is the authoritative full target schema; each later
phase's migration builds toward it incrementally, which is exactly what
the lookup-table / EAV-metadata extensibility design in that document is
for — adding a table or column later is additive, not a redesign.

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
  - `documents` (extraction_*, page_count, has_text_layer, needs_ocr,
    ocr_status) — extraction status, first-class and UI-visible rather
                                  than inferred (Phase 2 Step 1, see
                                  docs/PHASE_2_PLAN.md §12.1).
  - `document_pages`           — page-level extracted text, with a
                                  `source_sha256` snapshot linking each
                                  page back to its source document's hash
                                  independent of the FK join (§12.2).
  - `citations`                 — the exact document/page/span reference
                                  primitive every later phase (facts,
                                  timeline, relationship graph) cites
                                  through exclusively (§12.4). First
                                  written by highlight creation in Step 4.
                                  `text_source`/`source_confidence` (Phase
                                  3 Step 1) snapshot which kind of text a
                                  citation quoted, permanently — see the
                                  Citation docstring.
  - `tags` / `document_tags`   — case-scoped labels a user attaches to
                                  documents (Phase 2 Step 3). No rename or
                                  delete UI yet — see the Tag docstring.
  - `annotation_types` /
    `annotations`               — highlights, notes, and bookmarks (Phase 2
                                  Step 4). A highlight always creates a
                                  `citations` row too — see the Annotation
                                  docstring.
  - `ocr_jobs`                  — job-queue infrastructure for OCR
                                  execution (Phase 3 Step 0), first
                                  written to by real OCR code in Step 1.
  - `ocr_text_history`          — append-only archive of superseded raw
                                  OCR text (Phase 3 Step 1) — see the
                                  OcrTextHistory docstring.
  - `ocr_corrections`           — append-only human corrections to OCR
                                  text (Phase 3 Step 3). Never overwrites
                                  `document_pages.ocr_text` — see the
                                  OcrCorrection docstring.
  - `fact_types`                — lookup table for the Fact, Observation &
                                  Summary Layer (Phase 3.5 Step 1).
  - `ai_observations` /
    `ai_observation_citations`  — machine-suggested candidate facts,
                                  pending human review. Never read by the
                                  timeline/conflict tracker/binder — see
                                  the AiObservation docstring.
  - `verified_facts` /
    `verified_fact_citations`   — human-confirmed facts. The only table
                                  later phases (timeline, conflicts,
                                  binder) may read from this layer — see
                                  the VerifiedFact docstring.
  - `ai_summaries` /
    `summary_source_documents`  — schema for narrative AI summaries.
                                  Created now for schema completeness;
                                  no generator or review UI exists yet
                                  (Phase 3.5 Step 1) — see the AiSummary
                                  docstring.
  - `event_types`               — lookup table for timeline event kinds
                                  (Phase 4 Step 1).
  - `timeline_events` /
    `timeline_event_facts`      — the timeline schema. Every event has
                                  exactly one date-source verified fact
                                  (Phase 4 Step 1); no core module or UI
                                  exists yet — see the TimelineEvent
                                  docstring.
  - `app_auth`                  — the single owner's authentication state
                                  (Security Phase Step 1, see
                                  app/core/auth/passwords.py). A
                                  singleton table: zero rows before
                                  first-run setup, one row after.
  - `app_sessions`               — active login sessions (Security Phase
                                  Step 1). Deliberately not append-only,
                                  unlike every other table here -- see the
                                  AppSession docstring for why.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
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
    """A single matter/dispute — the top-level container for its documents.

    Doubles as the student-record boundary in the FERChronos UX refinement
    (Step 2): `legal_first_name`/`legal_last_name`/`preferred_name` are
    optional, nullable columns added additively on top of the original
    Phase 1 schema — `label` is untouched and remains the required,
    always-present field every existing/legacy row already has. See
    `display_name` below for how the two are reconciled for display.
    """

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

    # Optional student-name breakdown (FERChronos UX Step 2). All three are
    # nullable and independent of `label` — filling them in never changes
    # `label`, and leaving them blank (every case row created before this
    # step, or any student where a family just wants a plain name) is fully
    # supported. Stored as three separate fields, not one "legal_name"
    # string, specifically so `display_name` can place the preferred name
    # between the legal first and last name without parsing a free-text
    # name apart.
    legal_first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    legal_last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preferred_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    version_groups: Mapped[list["DocumentVersionGroup"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        """The student's name as shown throughout the UI.

        "LegalFirst (Preferred) LegalLast" — e.g. "Isabella (Izzy)
        Froiseth" — only when all three optional name fields are set.
        Otherwise falls back to `label`, which is always present. Never
        renders a "legal:" prefix or label — the preferred name simply
        sits in parentheses between the legal first and last name.
        """
        if self.legal_first_name and self.legal_last_name and self.preferred_name:
            return f"{self.legal_first_name} ({self.preferred_name}) {self.legal_last_name}"
        return self.label


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

    # When the family received *this copy* of the record -- distinct from
    # `document_date` above, which is the date on/about the record itself
    # (FERChronos UX refinement Step 4). Nullable, no source/precision/range
    # concept of its own (unlike document_date): it's a single plain date a
    # person enters directly, or leaves unset if unknown. Never inferred
    # from `ingested_at` (when this app happened to see the file) or any
    # filesystem timestamp.
    date_received: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    # --- Extraction status (Phase 2 Step 1, docs/PHASE_2_PLAN.md §4/§12.1) ---
    # extraction_status is the "did the extraction process itself run and
    # complete" axis: pending / completed / failed / unsupported_format.
    # needs_ocr is a separate, orthogonal axis -- "does some page's
    # *content* need OCR" -- true regardless of whether extraction
    # completed cleanly (e.g. a PDF can extract successfully overall while
    # some of its pages are scanned images needing OCR).
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_text_layer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # server_default (not just the Python-side `default=`) is required here,
    # not optional: these columns are added to an already-populated table
    # by a later migration, and SQLite refuses to ALTER TABLE ADD COLUMN
    # ... NOT NULL without a default when existing rows would otherwise get
    # NULL. Discovered by testing the migration against a non-empty table.
    needs_ocr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Reserved for Phase 3 (OCR execution): not_needed / queued / done / failed.
    # Phase 2 never sets this to anything but null.
    ocr_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    extraction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentPage.page_number",
    )
    tag_links: Mapped[list["DocumentTag"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
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


class DocumentPage(Base):
    """One page's worth of extracted and/or OCR'd text.

    See docs/DATA_MODEL.md "document_pages", docs/PHASE_2_PLAN.md §4/§12,
    and docs/PHASE_3_IMPLEMENTATION_PLAN.md §2/§7. `extracted_text` is
    populated by native extraction (Phase 2) and never touched by OCR.
    `ocr_text`/`extraction_confidence`/`ocr_word_boxes` are written by
    Phase 3 Step 1's `run_ocr_job()` -- `ocr_text` holds the *current* raw
    OCR value only; a value it replaces on a reprocess is archived to
    `ocr_text_history` first, never simply overwritten in place (see that
    model's docstring and docs/PHASE_3_DECISIONS.md §9.3/§10.2).
    """

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_page_number"),
    )

    page_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), nullable=False
    )

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(20), nullable=False)  # native/ocr/none
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Tesseract's word-level bounding boxes for this page's current
    # ocr_text, as a JSON list of {text, left, top, width, height,
    # confidence} objects -- captured for a future spatial-highlighting
    # phase (Phase 2 approved decision 2 keeps the viewer text-offset-only
    # for now); unread by any Phase 3 UI. See
    # docs/PHASE_3_DECISIONS.md §6.
    ocr_word_boxes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # A snapshot of documents.sha256_hash at the moment this page was
    # extracted, independent of the document_id FK -- see
    # docs/PHASE_2_PLAN.md §12.2. Should always equal the parent
    # document's current hash, since documents are immutable after
    # creation; a divergence would indicate something worth investigating.
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped["Document"] = relationship(back_populates="pages")
    # Ordered oldest-first so `corrections[-1]` is always the current
    # (most recent) correction -- see effective_text() in
    # app/core/ocr/text.py, the only place this ordering is relied on.
    corrections: Mapped[list["OcrCorrection"]] = relationship(
        back_populates="page", order_by="OcrCorrection.corrected_at"
    )


class Citation(Base):
    """The exact document/page/span reference primitive.

    See docs/DATA_MODEL.md "citations" and docs/PHASE_2_PLAN.md §12.4:
    every later phase (verified facts, timeline events, the relationship
    graph) cites through this table exclusively — there is no competing
    reference mechanism. First written by highlight creation in Phase 2
    Step 4 (`create_highlight()`, native text only at that point).

    `text_source`/`source_confidence` (Phase 3 Step 1) are snapshotted
    once, at citation-creation time, by that same function, and never
    updated afterward -- there is no citation-edit code path anywhere in
    this application, for any column. A citation permanently records what
    it quoted and from which kind of source *at the time it was made*;
    a later correction or re-OCR of that page never retroactively changes
    it. See docs/PHASE_3_DECISIONS.md §9.2/§10.3.
    """

    __tablename__ = "citations"

    citation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), nullable=False
    )
    page_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_pages.page_id"), nullable=True
    )

    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bounding_box: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quoted_text: Mapped[str] = mapped_column(Text, nullable=False)

    # native / ocr_raw / ocr_corrected -- which of effective_text()'s
    # branches (app/core/ocr/text.py) supplied quoted_text. NOT NULL with
    # server_default='native': every citation created before this column
    # existed was necessarily native (OCR didn't exist yet), so the
    # backfill is factually correct, not a guess -- see
    # docs/PHASE_3_DECISIONS.md §9.1.
    text_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="native", server_default="native"
    )
    # The page's extraction_confidence at the moment this citation was
    # made, if text_source is ocr_raw/ocr_corrected. Always NULL for
    # native (native extraction has no confidence score).
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    document: Mapped["Document"] = relationship()
    page: Mapped["DocumentPage | None"] = relationship()


class Tag(Base):
    """A case-scoped label a user can attach to documents.

    See docs/DATA_MODEL.md "tags" / "document_tags" and
    docs/PHASE_2_PLAN.md §13 Step 3. Tags are scoped per case (not
    global) and case-insensitively unique within a case, enforced by the
    application layer (see app/core/tagging.py) with this table's
    case-sensitive unique constraint as a database-level backstop. No
    rename/delete UI exists in Step 3 — a tag persists even once no
    document uses it; cleanup is deliberately out of scope for now.
    """

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("case_id", "name", name="uq_tag_case_name"),)

    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    case: Mapped["Case"] = relationship()


class DocumentTag(Base):
    """The many-to-many link between a document and a tag.

    A pure association row (composite primary key, no surrogate id) plus
    a timestamp — deliberately not a richer "tagging event" record, since
    app/core/tagging.py already writes a `tagged`/`untagged` custody
    event on the document for that purpose.
    """

    __tablename__ = "document_tags"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.tag_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped["Document"] = relationship(back_populates="tag_links")
    tag: Mapped["Tag"] = relationship()


class AnnotationType(Base):
    """Lookup table for annotation kinds (highlight / note / bookmark).

    Same extensibility pattern as `document_types` — a new kind is a row
    insert, not a migration. Seeded with defaults by app/db/seed.py.
    """

    __tablename__ = "annotation_types"

    type_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Annotation(Base):
    """A highlight, note, or bookmark a user attaches to a document (or a
    specific page/span of it).

    See docs/DATA_MODEL.md "annotations" and docs/PHASE_2_PLAN.md §7/§13
    Step 4. A highlight always carries a `citation_id` (the exact span it
    marks — see app/core/annotations/service.py, which derives
    `quoted_text` on that citation from the page's own stored text rather
    than trusting anything client-submitted). A note or bookmark may be
    page-scoped without a citation. Soft-delete only (`deleted_at`) — the
    linked citation, if any, is never touched by removing an annotation;
    see docs/PHASE_2_PLAN.md §12.4 on citations being permanent.

    Deliberately excluded from any future binder path and never usable as
    a citation source for a verified fact — see docs/DATA_MODEL.md
    "Annotations" for the full rationale. Not yet indexed for search in
    Step 4 — `annotation_notes_fts` is Step 5.
    """

    __tablename__ = "annotations"

    annotation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), nullable=False
    )
    page_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_pages.page_id"), nullable=True
    )
    citation_id: Mapped[int | None] = mapped_column(
        ForeignKey("citations.citation_id"), nullable=True
    )
    annotation_type_id: Mapped[int] = mapped_column(
        ForeignKey("annotation_types.type_id"), nullable=False
    )
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped["Document"] = relationship()
    page: Mapped["DocumentPage | None"] = relationship()
    citation: Mapped["Citation | None"] = relationship()
    annotation_type: Mapped["AnnotationType"] = relationship()


class OcrJob(Base):
    """One row per OCR run of one document (Phase 3 Step 0).

    See docs/PHASE_3_IMPLEMENTATION_PLAN.md §2. Unlike every other new
    table added in this application so far, this one is **not**
    append-only -- a job's own lifecycle (`queued` -> `running` ->
    `completed`/`completed_with_errors`/`failed`) is inherently a single
    evolving record, updated in place by the worker as that one run
    progresses, not a ledger of many rows. Historical OCR content itself
    (raw text superseded by a later run, corrections) lives in separate,
    genuinely append-only tables (`ocr_text_history`, `ocr_corrections`,
    both added in later steps) -- this table only ever tracks *job*
    status, not text content.

    Job *lifecycle* mechanics (claim/finish/crash-recovery) were built in
    Step 0 against a temporary placeholder processor; Step 1 replaces that
    placeholder with real OCR execution (`app/core/ocr/service.py`)
    without changing this table's shape at all.
    """

    __tablename__ = "ocr_jobs"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), nullable=False
    )

    # queued / running / completed / completed_with_errors / failed
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queued", server_default="queued"
    )
    engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped["Document"] = relationship()


class OcrTextHistory(Base):
    """Archive of a page's raw OCR text, superseded by a later reprocess.

    See docs/PHASE_3_IMPLEMENTATION_PLAN.md §2 and
    docs/PHASE_3_DECISIONS.md §9.3. Append-only -- written exactly once,
    by `run_ocr_job()` (app/core/ocr/service.py), immediately *before* it
    overwrites a page's existing (non-null) `ocr_text` on a reprocess run.
    Never written on a page's first OCR run (nothing to archive yet) and
    never updated or deleted afterward. Exists specifically so a
    superseded raw OCR guess is not simply lost the way a superseded
    `document_pages` row already is on native re-extraction (Phase 2 Step
    1) -- OCR output is not deterministic run-to-run the way re-running
    native extraction against an unchanged file is, so this phase gives
    it a stronger retention guarantee. Read only for audit/history
    display -- never consulted by `effective_text()`
    (app/core/ocr/text.py), which only ever reflects current state.
    """

    __tablename__ = "ocr_text_history"

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("document_pages.page_id"), nullable=False
    )

    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_by_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("ocr_jobs.job_id"), nullable=True
    )

    page: Mapped["DocumentPage"] = relationship()
    superseded_by_job: Mapped["OcrJob | None"] = relationship()


class OcrCorrection(Base):
    """A human correction to a page's OCR text (Phase 3 Step 3).

    See docs/PHASE_3_IMPLEMENTATION_PLAN.md §2/§3 and
    docs/PHASE_3_DECISIONS.md §1/§5/§9.3. Append-only -- written by
    `app/core/ocr/corrections.py::create_ocr_correction()`, never updated
    or deleted. `document_pages.ocr_text` (the raw OCR output) is never
    touched by a correction -- `docs/PRIVACY_SECURITY.md` §3 locks this:
    "OCR corrections are stored as a separate ... layer, never as an
    overwrite of the original OCR output." A page can have many
    corrections over time; there is no `is_current` flag or
    `previous_correction_id` chain -- "the current correction" is simply
    the most recent row for a page, resolved by ordering
    (`DocumentPage.corrections`, see that relationship), never a
    maintained pointer. `app/core/ocr/text.py::effective_text()` is the
    only place that resolution happens.
    """

    __tablename__ = "ocr_corrections"

    correction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("document_pages.page_id"), nullable=False
    )

    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_by: Mapped[str] = mapped_column(String(200), nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    page: Mapped["DocumentPage"] = relationship(back_populates="corrections")


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


class FactType(Base):
    """Lookup table for the kind of claim a fact/observation represents.

    Same extensibility pattern as `document_types`/`annotation_types` — a
    new kind is a row insert, not a migration. Seeded with defaults by
    app/db/seed.py. Shared by both `verified_facts` and `ai_observations`
    (see docs/DATA_MODEL.md "fact_types") since the same taxonomy applies
    to a confirmed fact and to the machine-suggested candidate it may have
    been promoted from.
    """

    __tablename__ = "fact_types"

    type_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AiObservation(Base):
    """A machine-suggested candidate fact, pending human review (Phase 3.5).

    See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "ai_observations".
    **Never read by the timeline, conflict tracker, or binder narrative**
    -- those only ever read `verified_facts`; a row here is a suggestion,
    not a usable fact, until a human promotes it. `statement`/
    `confidence_score`/`method` are written once at creation and never
    changed afterward -- only `status`/`reviewed_by`/`reviewed_at`
    transition, by app/core/facts/service.py's `promote_observation()` or
    `reject_observation()`; the row itself is never deleted, so the
    suggestion-to-decision history is always auditable. v1's only
    observation source (Phase 3.5 Step 3) is a deterministic regex/
    heuristic date-parser run over `effective_text()` -- not an LLM or
    cloud service; see docs/PROJECT_PLAN.md decision #11.
    """

    __tablename__ = "ai_observations"

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)
    fact_type_id: Mapped[int] = mapped_column(ForeignKey("fact_types.type_id"), nullable=False)

    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    # Set only when fact_type is "date" -- the real date the deterministic
    # date-parser (app/core/facts/date_extraction.py) computed internally
    # before formatting it into `statement` text. Added in Phase 4 Step 0
    # so a promoted date fact can carry a structured date forward to
    # verified_facts.fact_date instead of a human re-typing it -- see
    # docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 0. Null for any other
    # fact type, or if a future non-date observation source doesn't have
    # a date to give.
    observed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pending_review / accepted / rejected
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending_review", server_default="pending_review"
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Communications Phase Step 7: set only for a suggestion generated
    # from an imported email (app/core/communications/timeline_suggestions.py),
    # null for every Document-sourced observation (v1's date-parser,
    # above). Deliberately NOT routed through `citations` -- that table
    # requires a non-null `document_id` plus page/offset/bounding-box
    # fields that describe a position within a Document's extracted
    # text, none of which apply to a deterministic metadata candidate
    # built from a Communication's own structured fields (sender,
    # subject, date). A Communication-sourced observation therefore has
    # zero citations, which every citation-touching code path in
    # app/core/facts/service.py already tolerates (an empty citation
    # list is copied as an empty list, never rejected) -- this column is
    # the only schema change that pipeline needed. `unique=True` is a
    # DB-level backstop for "a Communication never accumulates more than
    # one suggestion" (SQLite/standard SQL treats multiple NULLs as
    # distinct, so this never blocks Document-sourced rows) -- the
    # authoritative dedup check still lives in
    # app/core/communications/timeline_suggestions.py, which checks
    # regardless of status, not just this constraint.
    communication_id: Mapped[int | None] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=True, unique=True
    )

    case: Mapped["Case"] = relationship()
    fact_type: Mapped["FactType"] = relationship()
    communication: Mapped["Communication | None"] = relationship()
    # Read-only convenience view over ai_observation_citations -- never
    # written through (viewonly=True); create_ai_observation() is the
    # only place a citation is ever attached, via that association table
    # directly, per its own docstring.
    citations: Mapped[list["Citation"]] = relationship(
        secondary="ai_observation_citations", viewonly=True
    )


class AiObservationCitation(Base):
    """Many-to-many: which citation(s) support one AI observation.

    See docs/DATA_MODEL.md "ai_observation_citations". Attached atomically
    when the observation is created -- no code path adds a citation to an
    existing observation afterward.
    """

    __tablename__ = "ai_observation_citations"

    observation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_observations.observation_id"), primary_key=True
    )
    citation_id: Mapped[int] = mapped_column(
        ForeignKey("citations.citation_id"), primary_key=True
    )


class VerifiedFact(Base):
    """A discrete, human-confirmed factual claim (Phase 3.5).

    See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "verified_facts".
    **Only rows in this table are usable by the timeline (Phase 4),
    conflict tracker (Phase 5), and binder narrative (Phase 6)** --
    `ai_observations` is never read directly by any of those. Created
    either directly by a human citing a document
    (app/core/facts/service.py::create_verified_fact()) or by promoting a
    reviewed `ai_observations` row (`promote_observation()`, which sets
    `source_observation_id` for lineage and leaves the originating
    observation row untouched -- it is never edited or deleted). Soft-
    delete only (`deleted_at`), same convention as `Document`/
    `Annotation` -- a fact is never hard-deleted once other rows may cite it.
    """

    __tablename__ = "verified_facts"

    fact_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)
    fact_type_id: Mapped[int] = mapped_column(ForeignKey("fact_types.type_id"), nullable=False)

    statement: Mapped[str] = mapped_column(Text, nullable=False)
    # certain / probable / uncertain -- the human's own certainty about the
    # fact, always required regardless of where the fact came from.
    confidence_label: Mapped[str] = mapped_column(String(20), nullable=False)
    # Only populated when this fact was promoted from (or otherwise
    # informed by) a scored ai_observations row -- null for a fact a human
    # asserted directly with no machine suggestion behind it.
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Non-null if and only if fact_type is "date" -- enforced by
    # app/core/facts/service.py, not a DB constraint (same style as every
    # other validation in this layer). This is the single source of
    # truth Phase 4's timeline reads an event's date from -- never
    # re-derived from `statement` text, never re-entered by a human a
    # second time. See docs/PHASE_4_IMPLEMENTATION_PLAN.md §1 decision 1.
    fact_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_observations.observation_id"), nullable=True
    )
    # Communications Phase Step 7: mirrors AiObservation.communication_id
    # -- direct traceability back to the source email that does not rely
    # on `source_observation_id` staying populated. Copied from
    # `source_observation.communication_id` by `promote_observation()`
    # when promoting a Communication-sourced observation; null for every
    # Document-sourced fact and for a fact a human asserted directly.
    communication_id: Mapped[int | None] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=True
    )

    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped["Case"] = relationship()
    fact_type: Mapped["FactType"] = relationship()
    source_observation: Mapped["AiObservation | None"] = relationship()
    communication: Mapped["Communication | None"] = relationship()
    # Read-only convenience view over verified_fact_citations -- never
    # written through (viewonly=True); create_verified_fact() and
    # promote_observation() are the only places a citation is ever
    # attached, via that association table directly.
    citations: Mapped[list["Citation"]] = relationship(
        secondary="verified_fact_citations", viewonly=True
    )


class VerifiedFactCitation(Base):
    """Many-to-many: which citation(s) support one verified fact.

    See docs/DATA_MODEL.md "verified_fact_citations". Attached atomically
    when the fact is created -- no code path adds a citation to an
    existing fact afterward.
    """

    __tablename__ = "verified_fact_citations"

    fact_id: Mapped[int] = mapped_column(ForeignKey("verified_facts.fact_id"), primary_key=True)
    citation_id: Mapped[int] = mapped_column(
        ForeignKey("citations.citation_id"), primary_key=True
    )


class AiSummary(Base):
    """Narrative AI-generated text about a document, case, or date range.

    See docs/ARCHITECTURE.md §3.7 and docs/DATA_MODEL.md "ai_summaries".
    **Schema-only in Phase 3.5** -- no code path in this application
    generates a row here yet, since doing so would require either a cloud
    AI service (categorically excluded, see docs/PRIVACY_SECURITY.md) or a
    local summarization method not yet approved (reserved for Phase 8,
    docs/PROJECT_PLAN.md). Structurally and permanently separate from
    `verified_facts` -- there is no code path that turns a row in this
    table into a verified fact, reviewed or not; a summary is interpretive
    synthesis, not a discrete citable claim. `label_text` is not free text
    -- a fixed, app-enforced constant a future generator writes verbatim,
    to be rendered wherever the summary appears, on screen or in an
    exported binder, regardless of `review_status`.
    """

    __tablename__ = "ai_summaries"

    summary_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)

    # document / case / timeline_range
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.document_id"), nullable=True
    )
    scope_range_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scope_range_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # pending_review / reviewed_accurate / reviewed_needs_correction
    review_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_review", server_default="pending_review"
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    label_text: Mapped[str] = mapped_column(Text, nullable=False)

    case: Mapped["Case"] = relationship()
    scope_document: Mapped["Document | None"] = relationship()


class SummarySourceDocument(Base):
    """Which documents fed into a given AI summary -- provenance for summaries.

    See docs/DATA_MODEL.md "summary_source_documents". Schema-only in
    Phase 3.5, same as `AiSummary` -- no writer exists yet.
    """

    __tablename__ = "summary_source_documents"

    summary_id: Mapped[int] = mapped_column(
        ForeignKey("ai_summaries.summary_id"), primary_key=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.document_id"), primary_key=True
    )


class EventType(Base):
    """Lookup table for the kind of timeline event (meeting, evaluation, ...).

    Same extensibility pattern as `fact_types`/`document_types` — a new
    kind is a row insert, not a migration. Seeded with defaults by
    app/db/seed.py. See docs/DATA_MODEL.md "event_types" and
    docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 1.
    """

    __tablename__ = "event_types"

    type_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TimelineEvent(Base):
    """A dated entry on a case's timeline (Phase 4 Step 1).

    See docs/ARCHITECTURE.md §3.8 and docs/PHASE_4_IMPLEMENTATION_PLAN.md
    §1/§2/§3. Every event has exactly one **date-source fact** -- a
    `verified_facts` row with `fact_type='date'` and a non-null
    `fact_date` -- recorded in `timeline_event_facts` with
    `is_date_source=True`, plus zero or more supporting facts of any
    type. `event_date` is copied from that fact's `fact_date` at
    creation time by `app/core/timeline/service.py::create_timeline_event()`
    (Step 2, not built yet) and never changes afterward -- an event
    cannot be re-anchored to a different fact in v1, only removed via
    soft-delete (`deleted_at`) and recreated if a correction is needed.

    `created_by` is always `"manual"` and `status` is always
    `"confirmed"` in v1 -- no system-suggested-event workflow exists
    (docs/PHASE_4_IMPLEMENTATION_PLAN.md §1 decision 2); the columns
    stay in the schema, matching docs/DATA_MODEL.md, for a possible
    future suggestion queue. `event_date_source` is always the fixed
    string `"verified_fact"` in v1, for the same reason `document_date_source`
    exists as a column even though Phase 1/2 only ever set it to
    `"manual"`.

    `event_date_precision`/`event_date_range_end` follow the same date
    representation pattern as `Document.document_date_precision`/
    `document_date_range_end` -- entered independently by the human at
    event creation, since `verified_facts.fact_date` is a single
    point-in-time value with no precision/range concept of its own.

    Never read from `document_pages`/`citations`/`ai_observations`
    directly -- only through an already-human-confirmed `verified_facts`
    row, via `timeline_event_facts`. No core module or UI exists yet
    (Steps 2-3).
    """

    __tablename__ = "timeline_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.case_id"), nullable=False)

    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_date_range_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    event_date_precision: Mapped[str] = mapped_column(
        String(20), nullable=False, default="exact"
    )
    # Always "verified_fact" in v1 -- see class docstring.
    event_date_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="verified_fact"
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.type_id"), nullable=False)

    # system-suggested / manual -- always "manual" in v1, see class docstring.
    created_by: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # confirmed / needs_review -- always "confirmed" in v1, see class docstring.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped["Case"] = relationship()
    event_type: Mapped["EventType"] = relationship()


class TimelineEventFact(Base):
    """Many-to-many: which verified fact(s) support one timeline event.

    See docs/DATA_MODEL.md "timeline_event_facts" and
    docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 1. Exactly one row per
    event has `is_date_source=True` -- the fact `event_date` was copied
    from at creation -- enforced by
    app/core/timeline/service.py::create_timeline_event() (Step 2, not
    built yet), not a DB constraint, same style as "at least one
    citation" in Phase 3.5. Every other attached row is
    `is_date_source=False`, whether attached at creation or later via
    `attach_fact_to_event()` (incremental attachment, approved for v1 --
    docs/PHASE_4_IMPLEMENTATION_PLAN.md §1 decision 4).
    """

    __tablename__ = "timeline_event_facts"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("timeline_events.event_id"), primary_key=True
    )
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("verified_facts.fact_id"), primary_key=True
    )
    is_date_source: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AppAuth(Base):
    """The single owner's authentication state (Security Phase Step 1).

    A singleton table -- FERChronos has exactly one local owner, never a
    multi-user login system (see docs/PRIVACY_SECURITY.md §6), so this
    table has zero rows before first-run setup and exactly one row
    afterward. "Has any row?" is how the app detects first launch (Step
    2) -- there is deliberately no separate "is_configured" flag to keep
    out of sync with reality.

    `password_hash`/`recovery_key_hash` are Argon2id-encoded hash strings
    from app/core/auth/passwords.py -- never the plaintext password or
    recovery key, which this application never stores or logs anywhere.
    `recovery_key_hash` is nullable at the schema level for one real
    reason even though setup (Step 5, app/api/auth.py::post_setup) always
    sets it in the same transaction as `password_hash` going forward: an
    account created before Step 5 shipped has no recovery key until the
    owner logs in with their existing password and generates one via
    app/api/account.py.

    `failed_login_attempts`/`locked_until` back the local login rate
    limiter (Step 4, app/api/auth.py): incremented on each failed
    attempt, reset to 0 on success, `locked_until` set to an escalating
    future timestamp after repeated failures. Step 5's recovery-key
    verification shares this exact same counter/lock -- see
    app/api/auth.py's module docstring.

    `inactivity_lock_minutes`/`session_absolute_expiry_hours` are the
    owner-configurable session-lifetime defaults (approved: 15 minutes /
    12 hours) -- kept on this singleton row rather than a separate
    settings table since there is only ever one row to hold them.
    """

    __tablename__ = "app_auth"

    auth_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    recovery_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inactivity_lock_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    session_absolute_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    password_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_key_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AppSession(Base):
    """One active login session (Security Phase Step 1).

    Server-side session state, not a signed client-side cookie -- the
    cookie holds only this row's opaque `session_id`
    (`secrets.token_urlsafe`, minted solely at successful login, never
    reused from a pre-auth visitor, which is what rules out session
    fixation by construction). A request is authenticated iff its cookie
    names a row here with `last_activity_at` within
    `AppAuth.inactivity_lock_minutes` and `expires_at` still in the
    future (see app.core.auth.enforcement.AuthEnforcementMiddleware and
    app.core.auth.session.resolve_and_maintain_session()).

    Deliberately **not** append-only, unlike every evidence-bearing table
    in this application: a session row is ephemeral security state, not
    an educational record, so logout/manual lock/expiry hard-delete the
    row rather than soft-deleting it (see docs/DATA_MODEL.md and
    docs/PRIVACY_SECURITY.md for why every other table in this schema
    preserves history instead). This is a deliberate, approved exception,
    not an oversight -- do not change other tables to match this one.
    """

    __tablename__ = "app_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Communications / Email Import Phase, Step 1 (schema only) -------------
#
# See docs/COMMUNICATIONS_PLAN.md for the full investigation and plan this
# schema implements Step 1 of. No core module, service logic, or UI exists
# yet for any of the tables below -- that is Steps 2+. This step exists
# purely so the shape of the data is settled, reviewed, and migrated before
# anything is built on top of it, matching how every prior phase's Step 1
# (e.g. timeline_events, ocr_jobs, ai_observations) landed schema-first.
#
# Naming is deliberately "communication_*"/"communications", not "email_*":
# `Communication.communication_type` is a discriminator column (fixed at
# "email" for everything this phase produces) so a later communication type
# (e.g. an imported SMS/chat export) can reuse this same table family
# without a schema redesign -- see the plan's §1 "Communications
# architecture" requirement.
#
# Every table here is purely additive -- no existing table gains, loses, or
# changes a column. `Communication`/`CommunicationAttachment` follow the
# same hashing/read-only-original/append-only-custody-ledger conventions as
# `Document`/`DocumentCustodyEvent`; `CommunicationImportBatch`/
# `CommunicationImportBatchItem` follow the same mutable job-lifecycle
# pattern as `OcrJob` (process state, not evidentiary content, so updating
# rows in place is correct here the same way it is there).


class CommunicationAccount(Base):
    """One connected mailbox (or, later, other communication source).

    `credential_ref` is an opaque lookup key into the OS-native secure
    credential store (see the Communications plan §3) -- e.g.
    `"ferchronos-yahoo-<account_id>"` -- never the credential itself.
    **No authentication secret of any kind is ever stored in this table,
    or anywhere else in this database.** `auth_method` is `"app_password"`
    for the only method this phase implements (a Yahoo-generated app
    password over read-only IMAP, per the plan's §2 decision); the column
    exists as a string, not a fixed enum, specifically so an `"oauth2"`
    value can be added later *if* Yahoo approves that access -- without a
    schema change -- while nothing here builds toward that speculatively.

    Not a singleton -- multiple mailboxes (or, later, non-email accounts)
    are supported by this shape from the start, even though Step 1 builds
    no UI to add a second one yet.

    Disconnecting (Communications plan §16/§4) clears the credential from
    the OS store and sets `status`/`disconnected_at` here -- it never
    deletes this row or any `Communication`/`CommunicationAttachment` row
    that came from it, so previously imported data survives a disconnect.
    """

    __tablename__ = "communication_accounts"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    # connected / disconnected
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="connected", server_default="connected"
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    communications: Mapped[list["Communication"]] = relationship(back_populates="account")


class CommunicationThread(Base):
    """A reconstructed conversation -- see the Communications plan §9.

    Populated by the (not-yet-built, Step 4) threading algorithm from
    Message-ID/In-Reply-To/References, with subject/participants/date as a
    fallback only for messages missing those headers. Never merges or
    rewrites the individual `Communication` rows it groups -- each
    message's own provenance is untouched regardless of thread membership,
    per the plan's explicit "never destroy individual-message provenance by
    merging originals" requirement. `message_count`/`first_message_at`/
    `last_message_at` are maintained by whatever service code creates/
    extends a thread (Step 4) -- there is no trigger or constraint enforcing
    them at the database level, consistent with every other derived/
    maintained-by-application-logic field in this schema (e.g.
    `DocumentVersionGroup.current_document_id`).
    """

    __tablename__ = "communication_threads"

    thread_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.case_id"), nullable=True)
    subject_normalized: Mapped[str | None] = mapped_column(String(998), nullable=True)
    participant_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    case: Mapped["Case | None"] = relationship()
    communications: Mapped[list["Communication"]] = relationship(back_populates="thread")


class Communication(Base):
    """One imported message -- the email-world analog of `Document`.

    See the Communications plan §4/§5. `sha256_hash`/`stored_path`/
    `file_size_bytes` describe the preserved raw RFC 822/MIME (`.eml`)
    bytes, hashed and stored read-only exactly like a document original
    (`app/core/files.py::compute_sha256`/`make_read_only`,
    `app/core/vault.py`) -- never rewritten, never the FERChronos-generated
    content itself. `body_text`/`body_html` are the *original* message
    content as parsed from those bytes; any FERChronos-generated summary,
    fact, or timeline suggestion derived from a communication lives in the
    existing `ai_observations`/`verified_facts`/`timeline_events` tables,
    linked back by FK, never merged into this row -- the same AI-inference
    boundary already enforced for documents (`docs/PRIVACY_SECURITY.md`
    §10) applies here unchanged, not as a new rule.

    `account_id` is nullable specifically so manual `.eml` upload (Step 3)
    never requires a connected mailbox -- the plan's explicit "keep manual
    import completely independent of Yahoo configuration" requirement.
    `case_id` is nullable until batch/individual assignment (plan §10).

    `message_id_header` is the RFC 5322 Message-ID this row's dedup
    primarily keys on (plan §7); `bcc_addresses` is populated only when a
    BCC header is actually present in the source bytes -- never inferred.
    `raw_headers` preserves the full original header block (as an ordered
    list of `{name, value}` objects, so repeated header names, e.g.
    multiple `Received:` lines, are not lost) for provenance, independent
    of the five headers `app/core/extraction/email.py` already parses out
    individually today.

    Soft-delete only (`deleted_at`), matching `Document`/`Annotation`/
    `VerifiedFact` -- a communication is never hard-deleted once other
    rows (custody events, attachments, links) may reference it.
    """

    __tablename__ = "communications"
    __table_args__ = (
        # Partial unique index: only enforced when a Message-ID is
        # actually present. SQLite treats every NULL as distinct, so
        # messages with no Message-ID header (rare, but real) never
        # collide here -- their dedup instead falls back to
        # (account_id, sha256_hash), checked in application logic (plan
        # §7), not a database constraint, since two accounts legitimately
        # importing the same message-with-no-id is not itself an error.
        Index(
            "uq_communication_account_message_id",
            "account_id",
            "message_id_header",
            unique=True,
            sqlite_where=text("message_id_header IS NOT NULL"),
        ),
    )

    communication_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    communication_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="email", server_default="email"
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_accounts.account_id"), nullable=True
    )
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.case_id"), nullable=True)

    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    from_display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    to_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cc_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Only ever populated when a BCC header is genuinely present in the
    # source bytes (almost never, for a received message) -- see class
    # docstring.
    bcc_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    message_id_header: Mapped[str | None] = mapped_column(String(998), nullable=True)
    in_reply_to_header: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references_header: Mapped[list | None] = mapped_column(JSON, nullable=True)
    raw_headers: Mapped[list | None] = mapped_column(JSON, nullable=True)

    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # IMAP provenance -- both null for a manually uploaded .eml, which has
    # no mailbox of origin.
    mailbox_uid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mailbox_folder: Mapped[str | None] = mapped_column(String(200), nullable=True)

    thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_threads.thread_id"), nullable=True
    )

    # manual_upload / mbox_import / imap_sync -- see docs/COMMUNICATIONS_PLAN.md
    # Step 8 for why "manual_upload" covers .eml specifically and
    # "mbox_import" is its own value: an mbox-derived message's raw
    # bytes are still the message's own genuine RFC822 original (mbox
    # only concatenates real messages, it doesn't transform them), but
    # provenance should still record that a human handed FERChronos an
    # archive rather than one individually-saved file.
    import_method: Mapped[str] = mapped_column(String(20), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    imported_by: Mapped[str] = mapped_column(String(200), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["CommunicationAccount | None"] = relationship(back_populates="communications")
    case: Mapped["Case | None"] = relationship()
    thread: Mapped["CommunicationThread | None"] = relationship(back_populates="communications")
    attachments: Mapped[list["CommunicationAttachment"]] = relationship(
        back_populates="communication", cascade="all, delete-orphan"
    )
    custody_events: Mapped[list["CommunicationCustodyEvent"]] = relationship(
        back_populates="communication",
        cascade="all, delete-orphan",
        order_by="CommunicationCustodyEvent.event_timestamp",
    )


class CommunicationAttachment(Base):
    """One attachment extracted from a `Communication`.

    See the Communications plan §6/§8. Hashed and stored read-only exactly
    like the parent message and like a `Document` original -- `sha256_hash`
    is computed *before* any classification or promotion decision, which is
    what makes duplicate-document detection possible without re-reading the
    file (plan §8: if a `Document` with this hash already exists in the
    target case, promoting this attachment links to that existing document
    via `CommunicationDocumentLink` rather than calling `ingest_document`
    again).

    `is_educational_record_candidate`/`suggested_document_type_id` are
    filled in by the (not-yet-built) local, deterministic classifier
    extending `app/core/document_type_suggestion.py` -- advisory only,
    same as every other suggestion in this application; never applied
    automatically. `review_status` starts `"pending"` and is set by the
    (not-yet-built) attachment review screen to `"added_to_documents"`,
    `"excluded"`, or `"left_with_email"` -- an attachment is never silently
    turned into a `Document` with no human decision recorded.
    """

    __tablename__ = "communication_attachments"

    attachment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    communication_id: Mapped[int] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=False
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    is_educational_record_candidate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    suggested_document_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_types.type_id"), nullable=True
    )
    # pending / added_to_documents / excluded / left_with_email
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    resulting_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.document_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    communication: Mapped["Communication"] = relationship(back_populates="attachments")
    suggested_document_type: Mapped["DocumentType | None"] = relationship()
    resulting_document: Mapped["Document | None"] = relationship()


class CommunicationDocumentLink(Base):
    """Provenance link between a `Document` and the communication/
    attachment it was promoted from -- deliberately many-to-many in
    effect, not a one-to-one pointer.

    See the Communications plan §11: if the same IEP arrives as an
    attachment on three separate emails, the *document* stays a single
    preserved file/row, while three of these link rows record each
    email's independent provenance (sender, date, import timestamp are all
    reachable from here via `communication_id`/`communication_attachment_id`,
    not duplicated onto this row). Append-only in practice -- there is no
    code path (planned) that updates or deletes a link once created, same
    spirit as `document_custody_events`, though this table itself is a
    plain provenance record rather than a ledger of state transitions.
    """

    __tablename__ = "communication_document_links"

    link_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.document_id"), nullable=False)
    communication_id: Mapped[int] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=False
    )
    communication_attachment_id: Mapped[int] = mapped_column(
        ForeignKey("communication_attachments.attachment_id"), nullable=False
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped["Document"] = relationship()
    communication: Mapped["Communication"] = relationship()
    communication_attachment: Mapped["CommunicationAttachment"] = relationship()


class CommunicationCustodyEvent(Base):
    """Append-only chain-of-custody ledger entry for one `Communication`.

    Mirrors `DocumentCustodyEvent` exactly -- same snapshot-fields-at-event
    convention, same "nothing in this application exposes an update or
    delete path for this table" guarantee. Kept as its own table rather
    than reusing `document_custody_events` since a `Communication` is not
    a `Document` and forcing it through that table's `document_id` FK
    would be a type mismatch, not a simplification.
    """

    __tablename__ = "communication_custody_events"

    custody_event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    communication_id: Mapped[int] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=False
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)

    sha256_hash_at_event: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes_at_event: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_location_at_event: Mapped[str] = mapped_column(String(1000), nullable=False)

    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    communication: Mapped["Communication"] = relationship(back_populates="custody_events")


class CommunicationImportBatch(Base):
    """One bulk-import run against a connected mailbox.

    See the Communications plan §14. Unlike `Communication` and every
    other evidentiary table above, this one is **not** append-only -- like
    `OcrJob`, it tracks the lifecycle of a background process (a batch's
    own progress), not evidentiary content, so updating its counters/status
    in place as the (not-yet-built) import worker progresses is correct
    here, not a deviation from this schema's usual conventions.
    `search_criteria` snapshots the filters (date range, sender, subject,
    keywords, folder, has-attachments, ...) the batch was run with, so a
    completed or in-progress batch's scope stays reviewable after the fact.
    """

    __tablename__ = "communication_import_batches"

    batch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("communication_accounts.account_id"), nullable=False
    )
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.case_id"), nullable=True)
    search_criteria: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # pending / running / paused / completed / completed_with_errors /
    # cancelled / failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_duplicate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    account: Mapped["CommunicationAccount"] = relationship()
    case: Mapped["Case | None"] = relationship()
    items: Mapped[list["CommunicationImportBatchItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class CommunicationImportBatchItem(Base):
    """One matched mailbox message within a `CommunicationImportBatch` --
    the unit of work that makes resumable/retry-safe bulk import possible.

    See the Communications plan §14. On restart, the (not-yet-built)
    import worker resumes by selecting `status == "pending"` items for an
    in-progress batch and continuing -- already-`imported`/
    `skipped_duplicate` items are never re-touched, so an interruption
    never requires restarting the whole batch. `communication_id` is set
    once this item results in a real `Communication` row (or left null for
    `skipped_duplicate`/`failed`). Mutable in place, same justification as
    `CommunicationImportBatch` itself.
    """

    __tablename__ = "communication_import_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "mailbox_uid", name="uq_import_batch_item_uid"),
    )

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("communication_import_batches.batch_id"), nullable=False
    )
    mailbox_uid: Mapped[str] = mapped_column(String(100), nullable=False)

    # pending / imported / skipped_duplicate / failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    communication_id: Mapped[int | None] = mapped_column(
        ForeignKey("communications.communication_id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped["CommunicationImportBatch"] = relationship(back_populates="items")
    communication: Mapped["Communication | None"] = relationship()
