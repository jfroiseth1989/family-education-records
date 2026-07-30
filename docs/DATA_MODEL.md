# FERPA Evidence Manager — Database Design (Phase 1)

Status: **Draft for review — no schema/migrations implemented yet.**

Single SQLite database per vault (`db.sqlite`), covering all cases. This is
a design-level entity list, not final SQL DDL — column types/constraints get
finalized when Phase 1 (Foundations) implementation begins.

## Design principles behind the schema

1. **Every derived fact must trace to an exact source.** Timeline events,
   binder entries, and citations never point at "a document" alone — they
   point at a specific page and, where possible, a specific span of text or
   bounding box within that page.
2. **OCR text is never conflated with native-extracted text.** They are
   different rows with a required `extraction_method`, so nothing downstream
   can accidentally treat a machine guess as verified document text.
3. **Nothing is hard-deleted.** Ingestion, edits, and exports are append-only
   in `audit_log`; document rows use soft-delete (`deleted_at`) so the audit
   trail and chain-of-custody story stay intact even if a user removes a
   document from view.
4. **Missing-record and conflict tracking are human-driven data structures,**
   not inference engines — the schema stores user/attorney-defined
   expectations and user-flagged conflicts, not automated legal judgments.
5. **Verified facts and AI-generated observations live in physically
   separate tables, not a shared table with a status flag.** A flag can be
   forgotten in a `WHERE` clause; separate tables mean code that queries
   "facts" for the timeline or binder cannot accidentally pull in an
   unreviewed AI guess — it would have to deliberately query a different
   table to do so. See "Fact, Observation & Summary Layer" below.
6. **Every fact-like record — human or AI in origin — carries a confidence
   level and at least one citation.** There is no code path that produces a
   fact without both.
7. **AI-generated summaries are narrative synthesis, not discrete facts, and
   can never be promoted into the verified-facts table.** Only discrete AI
   *observations* (a single suggested date, name, category, etc.) can be
   promoted to a verified fact, and only via explicit human confirmation.
   A summary paragraph is inherently interpretive — it stays a labeled
   summary forever, even after a human marks it reviewed.
8. **Record types, event types, and fact types are lookup-table rows, not
   hardcoded enums or schema columns.** Adding a new document type or event
   category in a future version is an `INSERT`, not a migration that
   touches existing tables. See "Extensibility" below.

## Entities

### `cases`
Top-level container (multi-case support, per your answer).
- `case_id` (PK)
- `label` — free text, e.g. "Jane Doe — 2025 IEP Dispute"
- `description`
- `status` — active / closed / archived
- `created_at`

### `documents`
One row per ingested file (including attachments extracted from emails).
- `document_id` (PK)
- `case_id` (FK → cases)
- `original_filename`
- `stored_path` — relative path under `originals/`
- `sha256_hash` — computed at ingest, re-verified before export
- `mime_type`, `file_size_bytes`, `page_count`
- `source` — free text, e.g. "emailed by district," "parent's own scan"
- `document_type_id` (FK → `document_types`) — see Extensibility below;
  replaces a hardcoded enum so new document types don't require a migration
- `document_date`, `document_date_precision` (exact/approximate/range),
  `document_date_source` (extracted/manual)
- `has_text_layer` (bool), `needs_ocr` (bool), `ocr_status`
- `ingested_at`, `ingested_by`
- `notes`
- `deleted_at` (nullable — soft delete)

### `document_pages`
Page-level text, one row per page per document.
- `page_id` (PK)
- `document_id` (FK)
- `page_number`
- `extracted_text` — native text-layer extraction (nullable)
- `ocr_text` — OCR output (nullable)
- `extraction_method` — native / ocr / none
- `extraction_confidence` — nullable, OCR only
- `char_count`
- `needs_ocr` (bool) — per-page flag; a document can be mixed (some native
  pages, some scanned pages needing OCR)

### `citations` (a.k.a. text spans)
The reusable "exact source reference" primitive used by timeline events,
binder entries, and tags.
- `citation_id` (PK)
- `document_id` (FK)
- `page_id` (FK, nullable)
- `start_offset`, `end_offset` — character offsets within the page text
- `paragraph_index` — nullable, for DOCX-native paragraph citations
- `bounding_box` — nullable JSON, for image/PDF spatial citations
- `quoted_text` — the actual excerpt, stored redundantly for display/audit
  even if underlying text is later re-extracted

### Extensibility: lookup tables and generic metadata

These exist so future document types, event types, and fact types can be
added without redesigning the schema — a new type is a row insert, and a
new type-specific attribute is a key/value row, not a new column.

**`document_types`** — `type_id` (PK), `name` (e.g. "IEP", "504 Plan",
"Evaluation", "Correspondence", "Discipline", "Attendance", "Grades",
"Medical", "Legal Filing", "Audio Transcript", "Other"), `description`,
`is_active`. Seeded with a sensible default list; the user (or a later
version) can add rows without a migration. `documents.document_type_id` FKs here.

**`event_types`** — same pattern for `timeline_events.event_type_id`
(meeting, evaluation, incident, communication, decision, deadline, ...).

**`fact_types`** — same pattern for `verified_facts.fact_type_id` /
`ai_observations.fact_type_id` (date, person, decision, category, custom, ...).

**`document_metadata`** — `document_id` (FK), `key`, `value`. A generic
key/value table for attributes that only apply to some document types
(e.g. `audio_duration_seconds` for a future audio-transcript type, or
`spreadsheet_row_count` for a future attendance-spreadsheet type) without
adding nullable columns to `documents` for every hypothetical future format.

**Search extensibility.** `document_pages` and `citations` stay the
canonical text + location store. Today they're indexed by the FTS5 table
below; a future search capability (e.g. a local-only semantic/vector search)
would be an *additive* table — e.g. `embeddings(embedding_id, source_type,
source_id, vector, model_name)` referencing existing `page_id`/`citation_id`
values — not a change to the tables it indexes. Same pattern for any future
faceted search (by person, by date range, by type): it reads the existing
relational structure rather than requiring one.

### `tags` / `document_tags`
- `tags`: `tag_id` (PK), `case_id` (FK), `name`, `category`
- `document_tags`: `document_id` (FK), `tag_id` (FK)

### `people` / `document_people`
- `people`: `person_id` (PK), `case_id` (FK), `name`, `role` (teacher,
  administrator, parent, advocate, evaluator, etc.)
- `document_people`: `document_id` (FK), `person_id` (FK), `role_in_document`
  (author / recipient / mentioned)

## Fact, Observation & Summary Layer

This is the layer that answers requirements 1, 2, and 5: every fact carries
a confidence level and a citation, verified facts and AI observations are
physically separate tables, and AI summaries are permanently and visibly
distinct from both.

```
citation (raw pointer: document + page + span)
   │
   ├── verified_facts ──────────────┐   (human-confirmed; used by timeline,
   │        ▲                       │    conflicts, binder narrative)
   │        │ promoted_from         │
   │        │ (lineage, optional)   │
   └── ai_observations ─────────────┘   (machine-suggested; pending_review
            (never used directly by         until a human accepts it, at
             timeline/conflicts/binder        which point a NEW verified_fact
             narrative)                       row is created — the observation
                                               row is kept, not overwritten)

ai_summaries ── separate from both; narrative text, always labeled, never
                promotable to verified_facts under any review status
```

### `verified_facts`
A discrete, human-confirmed factual claim. **Only rows in this table are
usable by the timeline, conflict tracker, and binder narrative.**
- `fact_id` (PK)
- `case_id` (FK)
- `fact_type_id` (FK → `fact_types`)
- `statement` — human-readable, e.g. "IEP annual review meeting held"
- `confidence_label` — `certain` / `probable` / `uncertain` (the human's own
  certainty about the fact — e.g. a date confirmed by an explicit date on
  the document vs. inferred from surrounding correspondence)
- `confidence_score` — nullable numeric 0.0–1.0, populated when the fact
  originated from (or was informed by) a scored AI observation
- `source_observation_id` — nullable FK → `ai_observations.observation_id`;
  set when this fact was promoted from an AI suggestion, preserving lineage
  from suggestion to confirmed fact
- `created_by`, `created_at`
- `deleted_at` (soft delete, per design principle #3)

### `verified_fact_citations`
Many-to-many — a fact can (and often should) cite multiple sources.
- `fact_id` (FK)
- `citation_id` (FK)

### `ai_observations`
A machine-generated candidate fact. **Never read by the timeline, conflict
tracker, or binder narrative directly** — it must go through human review
and promotion into `verified_facts` first.
- `observation_id` (PK)
- `case_id` (FK)
- `fact_type_id` (FK → `fact_types`)
- `statement` — the suggested claim, e.g. "Possible meeting date: 2024-03-12"
- `confidence_score` — numeric 0.0–1.0, from the extraction/parsing method
- `method` — e.g. "regex-date-parse-v1", "tesseract-5.x" (what produced this)
- `status` — `pending_review` / `accepted` / `rejected`
- `reviewed_by`, `reviewed_at` — nullable until a human acts on it
- `created_at`

### `ai_observation_citations`
Same pattern as `verified_fact_citations`, for observations.
- `observation_id` (FK)
- `citation_id` (FK)

### `ai_summaries`
Narrative AI-generated text (e.g., "summarize this document" or "summarize
this date range"). Structurally and permanently separate from facts —
there is no code path that turns a row in this table into a `verified_fact`.
- `summary_id` (PK)
- `case_id` (FK)
- `scope` — `document` / `case` / `timeline_range`
- `scope_document_id` — nullable FK, when scope = document
- `scope_range_start`, `scope_range_end` — nullable, when scope = timeline_range
- `summary_text`
- `generated_by` — model/method name
- `generated_at`
- `review_status` — `pending_review` (default) / `reviewed_accurate` /
  `reviewed_needs_correction`
- `reviewed_by`, `reviewed_at` — nullable
- `label_text` — **not free text** — a fixed, app-enforced constant such as
  "AI-Generated Summary — Unverified, Requires Human Review. Not a Fact or
  Legal Conclusion." rendered verbatim wherever this summary appears
  (screen or binder), regardless of `review_status`

### `summary_source_documents`
Which documents fed into a given AI summary — the provenance record for
summaries specifically.
- `summary_id` (FK)
- `document_id` (FK)

### `timeline_events`
- `event_id` (PK)
- `case_id` (FK)
- `event_date`, `event_date_precision`
- `title`, `description`
- `event_type_id` (FK → `event_types`)
- `created_by` — system-suggested / manual
- `status` — confirmed / needs_review

### `timeline_event_facts`
Many-to-many — an event is built from one or more **verified facts** (never
directly from raw citations or AI observations; a fact already carries its
own citation(s) and confidence, so the event inherits full traceability
through it).
- `event_id` (FK)
- `fact_id` (FK → `verified_facts`)

### `record_requirements` (missing-records tracking)
User- or attorney-defined checklist, **not** a built-in legal rule engine.
- `requirement_id` (PK)
- `case_id` (FK)
- `description` — e.g. "Annual IEP review"
- `expected_date` or `recurrence_rule` — nullable, user-entered
- `satisfied` (bool)
- `satisfied_by_document_id` (FK, nullable)
- `notes`

### `conflicts` (conflicting-records tracking)
- `conflict_id` (PK)
- `case_id` (FK)
- `description`
- `status` — open / resolved
- `created_at`

### `conflict_facts`
Many-to-many, ≥2 verified facts per conflict (the contradicting claims —
built on `verified_facts` rather than raw citations so a flagged conflict
always carries confidence context, not just competing excerpts).
- `conflict_id` (FK)
- `fact_id` (FK → `verified_facts`)
- `note`

### `ocr_jobs`
- `job_id` (PK)
- `document_id` (FK)
- `status` — queued / running / done / failed
- `engine` — e.g. "tesseract-5.x"
- `started_at`, `finished_at`, `error`

### `binder_exports`
Reproducibility record for every generated binder — top-level metadata only;
per-section provenance lives in the two tables below (requirement 3: full
provenance for every generated report, down to which sources contributed to
each conclusion, not just "this document was somewhere in the binder").
- `export_id` (PK)
- `case_id` (FK)
- `generated_at`
- `file_path`, `sha256_hash`
- `template_version`

### `binder_sections`
One row per rendered section of a specific export (cover, TOC, exhibit N,
timeline entry N, AI-summary appendix entry N, ...) — the addressable units
that provenance attaches to.
- `section_id` (PK)
- `export_id` (FK)
- `section_type` — cover / toc / exhibit / timeline_entry / summary_appendix
- `title`
- `order_index`
- `content_ref` — nullable FK-like reference (e.g. `document_id`, `event_id`,
  or `summary_id`, per `section_type`)

### `binder_export_sources`
Which sources contributed to which section — the provenance manifest itself.
- `export_id` (FK)
- `section_id` (FK → `binder_sections`)
- `source_type` — `document` / `verified_fact` / `citation` / `ai_summary`
- `source_id`

An AI-summary section can only ever be a `summary_appendix`-type section
(see PRIVACY_SECURITY.md/ARCHITECTURE.md — AI summaries are barred from the
narrative/exhibit/timeline sections of the binder by generation logic, not
just by convention), and `binder_export_sources` rows of `source_type =
ai_summary` only ever attach to `summary_appendix` sections — making the
separation checkable by querying the export, not just by trusting the
template author.

### `audit_log`
Append-only; no UPDATE/DELETE exposed through the app.
- `log_id` (PK)
- `case_id` (FK, nullable)
- `event_type` — ingest / export / edit / delete / ocr_run
- `entity_type`, `entity_id`
- `actor`
- `timestamp`
- `details` (JSON)

## Full-text search

An FTS5 virtual table (`document_text_fts`) indexes `document_pages`
(`extracted_text` and `ocr_text`), external-content-linked back to
`page_id`, so search results resolve directly to a citable page.

## Entity relationship summary

```
document_types, event_types, fact_types  (lookup tables — extensibility)

cases ─┬─ documents ─┬─ document_pages ─┬─ citations ─┬─ verified_fact_citations ─ verified_facts ─┬─ timeline_event_facts ─ timeline_events
       │              │                  │             └─ ai_observation_citations ─ ai_observations   ├─ conflict_facts ─ conflicts
       │              │                  │                      (never read directly by timeline/       └─ binder_export_sources ─ binder_sections ─ binder_exports
       │              │                  │                       conflicts/binder; only via promotion
       │              │                  │                       into verified_facts, with lineage)
       │              │                  └─ document_text_fts (search index)
       │              ├─ document_tags ─ tags
       │              ├─ document_people ─ people
       │              ├─ document_metadata (EAV, extensibility)
       │              ├─ ocr_jobs
       │              └─ summary_source_documents ─ ai_summaries
       ├─ record_requirements
       └─ audit_log
```

Note the two structurally distinct paths out of `citations`: one through
`verified_facts` (human-confirmed, everything downstream can trust it) and
one through `ai_observations` (machine-suggested, dead-ends until a human
promotes it). `ai_summaries` doesn't sit on this graph at all — it's
narrative text scoped to documents/ranges, permanently ineligible for
promotion, which is what requirement 5 calls for.
