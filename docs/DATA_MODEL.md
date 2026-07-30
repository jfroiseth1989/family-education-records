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
9. **A corrected or updated record is a new, separate `documents` row —
   never an edit to an existing one.** Versioning is a *relationship*
   between immutable rows (`document_version_groups`, `supersedes_document_id`),
   not a mutation of a row's content, filename, or hash. Every previous
   version stays fully intact, on disk and in the database, indefinitely.
10. **Chain of custody is a first-class, structured ledger per document**
    (`document_custody_events`) — not something reconstructed after the
    fact from a generic activity log. Every document gets an `imported`
    entry recording filename, hash, size, and storage location at the
    moment of ingestion, and every subsequent action on that document
    (extraction, OCR, tagging, fact-linking, version superseding, export
    inclusion) appends a new row. It is append-only, like `audit_log`, and
    exists specifically so a single query against one document returns its
    complete custody history without joining across unrelated event types.
11. **User annotations never touch original file bytes.** Highlights,
    bookmarks, and notes are rows in `annotations` that reference a
    document/page/citation; they are rendered as an overlay at view time
    (e.g. a PDF.js annotation layer), never written into the source file.
    Deleting or editing an annotation cannot affect the original in any way.
12. **The relationship graph reuses the verified/AI-suggested split
    established for facts, rather than inventing a separate pattern.**
    `verified_relationships` (human-confirmed, cited, the only edges a
    graph view renders as established) and `ai_suggested_relationships`
    (machine-proposed, pending review, never rendered as an established
    edge) mirror `verified_facts` / `ai_observations` exactly. Every
    relationship — verified or suggested — must cite at least one source
    document; there is no code path that creates an uncited edge.

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
- `version_group_id` — nullable FK → `document_version_groups`. Null means
  this document has no known other versions (the common case). Set only
  when the user explicitly links this import to an existing document as a
  new (or prior) version.
- `version_number` — nullable integer, sequential within `version_group_id`
  (1, 2, 3, ...)
- `is_current_version` — bool, default `true`. Exactly one `true` per
  `version_group_id` at any time; promoting a new version to current flips
  the previous current version's row to `false` in the same transaction —
  it is never deleted or hidden, only relabeled.
- `supersedes_document_id` — nullable, self-referential FK → `documents`.
  Direct pointer to the immediately-prior version, so the chain is walkable
  even without querying the whole group.
- `version_note` — free text entered by the user at the time of linking,
  e.g. "Corrected evaluation date; district reissued 2025-02-01."

None of `original_filename`, `sha256_hash`, `stored_path`, or
`file_size_bytes` are ever updated in place on an existing row once set —
see design principle 9. A "corrected IEP" is always a brand-new row with its
own hash and its own file under `originals/`.

### `document_version_groups`
The stable identity for a "logical record" across its versions — e.g. "IEP
— Jane Doe" spans however many corrected/reissued files arrive over time.
- `group_id` (PK)
- `case_id` (FK)
- `label` — user-assigned, e.g. "IEP — Jane Doe"
- `created_at`
- `current_document_id` — FK → `documents.document_id`, denormalized
  pointer to whichever version currently has `is_current_version = true`,
  kept in sync by the app in the same transaction as any version change,
  so "what's the current version of X" is a single lookup rather than a
  scan over the group.

Grouping is opt-in and user-driven: ingesting a file never auto-detects
"this is probably a new version of that other file." The user (or their
attorney) explicitly links two documents as versions of each other — either
at import time ("this replaces an earlier document") or later from a
document's detail view ("link as a new version of..."). This is deliberate:
auto-matching similar-looking records is exactly the kind of judgment call
that belongs to the user, not a heuristic, given what's riding on getting it right.

### `document_custody_events`
The chain-of-custody ledger. One row per action taken on a document, from
import forward. Append-only — no `UPDATE`/`DELETE` exposed through the app,
same rule as `audit_log`. This is deliberately a dedicated, structurally
required table rather than reuse of `audit_log`'s generic JSON `details`
field: a chain-of-custody record needs its core fields (hash, size,
location) to be actual columns that are always present, not optionally
present inside a blob.
- `custody_event_id` (PK)
- `document_id` (FK)
- `event_type` — `imported` / `hash_verified` / `extracted` / `ocr_run` /
  `tagged` / `linked_to_fact` / `version_linked` / `version_superseded` /
  `included_in_export` / `annotated` / `soft_deleted` / `restored`
- `event_timestamp`
- `actor` — user identifier, or "system" for automated steps (e.g. an
  extraction job)
- `original_filename` — recorded on the `imported` event (and thereafter
  redundant with `documents.original_filename`, but kept here so the
  custody row is self-contained even if it's ever queried independently)
- `sha256_hash_at_event` — the hash as verified *at this event*, on every
  event type, not just `imported`. Because this is captured every time,
  the custody trail itself shows hash consistency (or flags a mismatch)
  across the document's entire history, rather than relying on a single
  hash check at ingestion.
- `file_size_bytes_at_event`
- `storage_location_at_event` — the file's path under the vault at the time
  of this event (originals are never moved, so in practice this is constant,
  but recording it at every event makes that an observed fact in the ledger,
  not an assumption)
- `details` — JSON, event-specific (e.g. which case/tag/fact/export an
  action relates to)

The `imported` event is created in the same transaction as the `documents`
row itself, so it is impossible for a document to exist in the system
without a corresponding first custody entry recording import date, original
filename, hash, size, and storage location — directly satisfying the
requirement that every imported document has a chain-of-custody record from
the moment it enters the vault.

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

**`organization_types`**, **`graph_entity_types`**, **`relationship_types`**,
**`service_types`**, **`annotation_types`** — same pattern, feeding the
relationship graph and annotation layer below. Notably, `graph_entity_types`
(seeded with `person` / `organization` / `document` / `timeline_event`) means
a future node kind — e.g. a `location` or `service_type` node — is a row
insert plus a new column-value pairing, not a schema change to the
relationship tables themselves.

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
  administrator, parent, advocate, evaluator, provider, etc. — a
  **provider** is simply a person with role `provider`; there's no separate
  provider table, to avoid two sources of truth for "who is this person")
- `document_people`: `document_id` (FK), `person_id` (FK), `role_in_document`
  (author / recipient / mentioned)

### `organizations` / `document_organizations`
Same pattern as `people`, for entities like a school district, clinic,
transportation company, or advocacy organization — many documents (letters,
service agreements) are from/to an organization rather than, or in addition
to, a named individual.
- `organizations`: `organization_id` (PK), `case_id` (FK), `name`,
  `organization_type_id` (FK → `organization_types` lookup table, e.g.
  school district, clinic, transportation provider, agency, other)
- `document_organizations`: `document_id` (FK), `organization_id` (FK),
  `role_in_document` (author / recipient / mentioned)

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

## Relationship Graph

Connects people, organizations, documents, meetings, evaluations, IEPs,
emails, incidents, transportation decisions, providers, services, and
timeline events. Rather than one table per noun in that list, the graph
uses a small, extensible set of **node types** — `person`, `organization`,
`document`, `timeline_event` — because the more specific concepts are
already represented within those: a meeting or an incident is a
`timeline_event` with the matching `event_type`; an evaluation or an IEP is
a `document` with the matching `document_type`; a provider is a `person` or
`organization` with role `provider`. This avoids two sources of truth for
the same underlying record. If a future version needs a node type that
genuinely isn't one of these (e.g. a physical `location`), it's a new row
in `graph_entity_types` plus edges referencing it — not a schema redesign.

### `verified_relationships`
An edge between two entities. **Only rows in this table render as an
established connection in the graph view.**
- `relationship_id` (PK)
- `case_id` (FK)
- `relationship_type_id` (FK → `relationship_types` — e.g. attended,
  authored, sent_to, evaluated_by, resulted_in, referenced_in,
  provides_service_to, transported_by, responsible_for, related_to)
- `from_entity_type` / `from_entity_id` (FK → `graph_entity_types`, plus the
  ID within that entity's own table)
- `to_entity_type` / `to_entity_id` — same pattern
- `service_type_id` — nullable FK → `service_types` (e.g. speech therapy,
  OT/PT, transportation, counseling), used when the relationship represents
  a provider-to-service-recipient connection
- `description` — free text, e.g. "Dr. Chen conducted the March 2024
  evaluation referenced in the May IEP"
- `confidence_label` — `certain` / `probable` / `uncertain` (same scale as
  `verified_facts`)
- `source_suggestion_id` — nullable FK → `ai_suggested_relationships`,
  lineage when this edge was promoted from a suggestion
- `created_by`, `created_at`, `deleted_at` (soft delete)

### `verified_relationship_citations`
Many-to-many — **every relationship must cite at least one source
document**; this is not optional. A relationship with zero rows here is not
a valid verified relationship.
- `relationship_id` (FK)
- `citation_id` (FK)

### `ai_suggested_relationships`
A machine-proposed edge. **Never rendered as an established connection** —
graph views show these, if at all, as visually distinct (e.g. dashed,
"Suggested — needs review") and never merge them into the main graph until
promoted. Mirrors `ai_observations` exactly.
- `suggestion_id` (PK)
- `case_id` (FK)
- `relationship_type_id` (FK)
- `from_entity_type` / `from_entity_id`, `to_entity_type` / `to_entity_id`
- `service_type_id` — nullable
- `description`
- `confidence_score` — numeric 0.0–1.0
- `method` — e.g. "shared-document-heuristic", "co-occurrence-in-page"
- `status` — `pending_review` / `accepted` / `rejected`
- `reviewed_by`, `reviewed_at`
- `created_at`

Promotion works identically to observations: a human accepting a suggestion
creates a new `verified_relationships` row with `source_suggestion_id` set;
the suggestion row is retained, not overwritten.

### `ai_suggested_relationship_citations`
Same pattern as `verified_relationship_citations`, for suggestions — even a
suggested relationship must point at whatever evidence (e.g. "these two
people appear on the same document") produced the suggestion.
- `suggestion_id` (FK)
- `citation_id` (FK)

**On the polymorphic `from_entity_type`/`to_entity_type` reference:** SQLite
can't natively foreign-key a column whose target table varies by another
column's value. v1 enforces "the referenced ID actually exists in the
matching table" at the application layer (validated in the same transaction
as any insert/update), not via a database constraint. This is a known
trade-off worth a deliberate decision before Phase 1 coding — see
PROJECT_PLAN.md.

**On automatic suggestion generation:** the requirement is that the system
*never* presents an inferred relationship as established without marking it
suggested and gating it on human approval — it does not require that v1
actually build a suggestion engine. A v1 relationship graph could be
entirely manual (the user draws every edge, each with a citation) and would
fully satisfy this requirement, since there's no inference happening at
all. Given there's no cloud AI in this system (PRIVACY_SECURITY.md),
any v1 suggestion engine would be a local heuristic (e.g., "these two
people are named on the same document") — see PROJECT_PLAN.md for whether
that's in scope for v1 or deferred.

## Annotations (Highlights, Bookmarks, Notes)

Answers the "preserve every document exactly as imported" requirement:
annotations are pure metadata that reference a document, optionally a page
or an exact citation span, and are rendered as an overlay at view time.
Nothing in this table, or any code path that writes to it, ever opens a
source file in write mode.

### `annotations`
- `annotation_id` (PK)
- `case_id` (FK)
- `document_id` (FK)
- `page_id` — nullable FK (e.g. a bookmark can be page-level, no specific span)
- `citation_id` — nullable FK (a highlight tied to an exact span reuses the
  same citation primitive everything else uses)
- `annotation_type_id` (FK → `annotation_types` — highlight / note / bookmark)
- `body_text` — nullable (highlights/bookmarks may carry no text; notes do)
- `color` — nullable, for highlight color-coding
- `created_by`, `created_at`, `updated_at`, `deleted_at`

Annotation text is indexed in its own `annotation_notes_fts` FTS5 table
(separate from `document_text_fts`, so a search can distinguish "found in
the source document" from "found in your own notes about it" — an important
distinction not to blur, given everything else in this system is built
around never confusing a source document's content with something added on top).

Annotations are personal working notes, not evidence: by default they are
**not** included in a generated binder (see `binder_export_sources` —
`annotation` is deliberately not one of its `source_type` values in v1) and
can never be cited by a `verified_fact` or `verified_relationship` as if
they were a source document. A user wanting to preserve the substance of a
note as part of the record would need to turn it into an actual fact/
relationship with its own citation, same as any other claim — this keeps
the "everything in the evidentiary output traces to a source document"
guarantee from PRIVACY_SECURITY.md intact.

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
Append-only; no UPDATE/DELETE exposed through the app. Covers case- and
system-level activity (case created, requirement added/edited, conflict
flagged, binder exported, vault backup taken). Document-level actions are
recorded in `document_custody_events` instead, which is deliberately more
rigorous (structured hash/size/location on every row) than this table's
generic `details` JSON — for a specific document's history, query
`document_custody_events`, not `audit_log`.
- `log_id` (PK)
- `case_id` (FK, nullable)
- `event_type` — export / edit / delete / requirement_change / conflict_flagged / backup
- `entity_type`, `entity_id`
- `actor`
- `timestamp`
- `details` (JSON)

## Full-text search

An FTS5 virtual table (`document_text_fts`) indexes `document_pages`
(`extracted_text` and `ocr_text`), external-content-linked back to
`page_id`, so search results resolve directly to a citable page. A second,
separate FTS5 table (`annotation_notes_fts`) indexes `annotations.body_text`
— kept apart from `document_text_fts` so search results are always
unambiguous about whether a hit is in a source document or in the user's
own notes about one.

## Entity relationship summary

```
document_types, event_types, fact_types  (lookup tables — extensibility)

document_version_groups ─(current_document_id)─► documents

cases ─┬─ documents ─┬─ document_pages ─┬─ citations ─┬─ verified_fact_citations ─ verified_facts ─┬─ timeline_event_facts ─ timeline_events
       │      ▲       │                  │             └─ ai_observation_citations ─ ai_observations   ├─ conflict_facts ─ conflicts
       │      │       │                  │                      (never read directly by timeline/       └─ binder_export_sources ─ binder_sections ─ binder_exports
       │      │       │                  │                       conflicts/binder; only via promotion
       │      │       │                  │                       into verified_facts, with lineage)
       │      │       │                  └─ document_text_fts (search index)
       │      │       ├─ document_tags ─ tags
       │      │       ├─ document_people ─ people
       │      │       ├─ document_organizations ─ organizations
       │      │       ├─ document_metadata (EAV, extensibility)
       │      │       ├─ document_custody_events (chain of custody, append-only)
       │      │       ├─ annotations ─ annotation_notes_fts (search index, separate from document text)
       │      │       └─ ocr_jobs
       │      └─ supersedes_document_id (self-referential — prior version chain)
       │
       ├─ document_version_groups
       ├─ record_requirements
       ├─ summary_source_documents ─ ai_summaries
       └─ audit_log (case/system-level activity; document activity lives in document_custody_events)
```

Relationship graph (its own diagram — every node type above can appear as
either endpoint of an edge):

```
graph_entity_types (person | organization | document | timeline_event)
relationship_types, service_types  (lookup tables)

verified_relationship_citations ─ verified_relationships ──(from/to entity)──► {people, organizations, documents, timeline_events}
        ▲                                  ▲
        │ citations                        │ source_suggestion_id (lineage)
        │                                  │
ai_suggested_relationship_citations ─ ai_suggested_relationships ──(from/to entity)──► {people, organizations, documents, timeline_events}
        (never rendered as an established edge; promotion creates a new
         verified_relationships row, same pattern as ai_observations)
```

Note the two structurally distinct paths out of `citations`: one through
`verified_facts` (human-confirmed, everything downstream can trust it) and
one through `ai_observations` (machine-suggested, dead-ends until a human
promotes it) — and the identical pattern repeated for
`verified_relationships` / `ai_suggested_relationships`. `ai_summaries`
doesn't sit on this graph at all — it's
narrative text scoped to documents/ranges, permanently ineligible for
promotion, which is what requirement 5 calls for.
