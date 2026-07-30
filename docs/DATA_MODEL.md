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
- `record_type` — IEP, 504, evaluation, correspondence, discipline,
  attendance, grades, medical, legal-filing, other (user-editable list)
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

### `tags` / `document_tags`
- `tags`: `tag_id` (PK), `case_id` (FK), `name`, `category`
- `document_tags`: `document_id` (FK), `tag_id` (FK)

### `people` / `document_people`
- `people`: `person_id` (PK), `case_id` (FK), `name`, `role` (teacher,
  administrator, parent, advocate, evaluator, etc.)
- `document_people`: `document_id` (FK), `person_id` (FK), `role_in_document`
  (author / recipient / mentioned)

### `timeline_events`
- `event_id` (PK)
- `case_id` (FK)
- `event_date`, `event_date_precision`
- `title`, `description`
- `event_type` — meeting, evaluation, incident, communication, decision,
  deadline, other
- `created_by` — system-suggested / manual
- `status` — confirmed / needs_review

### `timeline_event_citations`
Many-to-many — an event can (and often should) cite multiple sources.
- `event_id` (FK)
- `citation_id` (FK)

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

### `conflict_citations`
Many-to-many, ≥2 citations per conflict (the contradicting excerpts).
- `conflict_id` (FK)
- `citation_id` (FK)
- `note`

### `ocr_jobs`
- `job_id` (PK)
- `document_id` (FK)
- `status` — queued / running / done / failed
- `engine` — e.g. "tesseract-5.x"
- `started_at`, `finished_at`, `error`

### `binder_exports`
Reproducibility record for every generated binder.
- `export_id` (PK)
- `case_id` (FK)
- `generated_at`
- `file_path`, `sha256_hash`
- `included_document_ids` (JSON array)
- `included_event_ids` (JSON array)
- `template_version`

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
cases ─┬─ documents ─┬─ document_pages ─┬─ citations ─┬─ timeline_event_citations ─ timeline_events
       │              │                  │             └─ conflict_citations ─ conflicts
       │              │                  └─ document_text_fts (search index)
       │              ├─ document_tags ─ tags
       │              ├─ document_people ─ people
       │              └─ ocr_jobs
       ├─ record_requirements
       ├─ binder_exports
       └─ audit_log
```
