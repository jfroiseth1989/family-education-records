# IEP Consistency Review — Investigation & Architecture Plan

Status: **Draft for review. No code written. No migration written. Nothing in
this document has been applied to the schema.** This is the deliverable
requested: investigation + design only, to be approved or revised before any
implementation step begins — matching the same discipline every other phase
in this repo has followed (see COMMUNICATIONS_PLAN.md, PHASE_4_IMPLEMENTATION_PLAN.md).

## 0. Framing, and a correction to the aspirational schema docs

Before anything else: `docs/DATA_MODEL.md` and `docs/ARCHITECTURE.md` are
**Phase 1 planning documents**, not an up-to-date description of what's
actually built. Cross-checking them against `app/db/models.py` while
researching this plan turned up a real gap worth flagging now rather than
silently building on top of: neither document describes what's true today —
`people`, `organizations`, `document_metadata`, `record_requirements`,
`conflicts`/`conflict_facts`, the entire relationship graph
(`verified_relationships`/`ai_suggested_relationships`/`graph_entity_types`/
etc.), and the entire Evidence Binder (`binder_exports`/`binder_sections`/
`binder_export_sources`) **do not exist in the codebase.** What was actually
built, in order, is: Foundations → Extraction/Search/Annotations → OCR →
Fact/Observation Layer → Timeline → (a large, separately-planned)
Communications phase (Yahoo IMAP import, threading, attachment→Document
promotion, Communications search, timeline suggestions from email). The
"Phase 5 — Missing/Conflicting Records" concept ARCHITECTURE.md §3.10
describes — "No automated contradiction detection in v1 (flagged as a
stretch goal, local-only NLP)" — was never built either. **This plan is,
functionally, that stretch goal, arriving later and far more specifically
scoped than Phase 1 imagined it.**

This matters for the plan below in two ways: (1) every "reuse existing X"
claim in this document was verified against the actual model code, not the
aspirational docs, and (2) there is no relationship graph to lean on for
"how do two documents relate to each other" — Section 5 below has to
propose a narrowly-scoped equivalent rather than pointing at one that
already exists.

## 1. Current FERChronos components that can be reused

Verified directly against `app/db/models.py` and the relevant `app/core/`
modules (not the planning docs). Everything below is real and running today.

| Component | What it gives this feature |
|---|---|
| `Document` + `DocumentType` (`app/db/models.py`, `app/db/seed.py`) | `document_type_id` already distinguishes IEP / 504 Plan / Evaluation / Prior Written Notice / Progress Report / Meeting Notice / Correspondence, seeded by `app/db/seed.py::DEFAULT_DOCUMENT_TYPES`. This is the existing answer to "which documents are IEPs" (see §5). |
| `document_type_suggestion.py` | A deterministic, local, regex/phrase-trigger classifier (no AI, no network) already used to *suggest* a document's type from filename + text sample, always requiring human confirmation, returning `None` on ambiguity rather than guessing. This is the direct architectural precedent for every deterministic detector this plan proposes — same shape, same "explain what matched" discipline, same "no guess when ambiguous" discipline. |
| `Document.document_date`/`document_date_range_end`/`document_date_precision`/`document_date_source` | The existing date-representation pattern (exact/approximate/range, always-null-when-unknown). Already gives chronological ordering of IEPs within a case for free once dates are populated (manually or via the existing date-suggestion pipeline). |
| `DocumentVersionGroup`/`Document.supersedes_document_id`/`is_current_version` | Existing, **fully manual**, user-driven "this file replaces that one" linking. Deliberately *not* reused for IEP-to-IEP or IEP-to-amendment relationships (see §5) — its semantics ("exactly one current version, the others are superseded") don't match "this amendment modifies that annual IEP, both remain live records." |
| `DocumentPage` + `Citation` + `effective_text()` (`app/core/ocr/text.py`) | The exact-span provenance primitive already used everywhere (highlights, date observations, search results). `effective_text()` already resolves "what is this page's real text right now" (native → OCR raw → OCR corrected), so any new extractor reads through it and never has to re-derive that logic. Every structured field this plan extracts gets its own `Citation` row, exactly like a highlight or a date observation does. |
| `FactType`/`AiObservation`/`VerifiedFact` + `app/core/facts/service.py` | The established **verified-vs-AI-suggested split** (`docs/PRIVACY_SECURITY.md` §10's "AI inference boundary" rule): a machine-suggested candidate lives in its own table, is never read by downstream features as if confirmed, and promotion always creates a new row with lineage rather than mutating the suggestion. **Not directly reused as a schema for IEP structured data** (see §2 for why), but its *pattern* — and its exact promotion/lineage/audit-log-logging mechanics — is the direct template for the new `iep_inconsistency_flags` review lifecycle in §7. |
| `app/core/facts/date_extraction.py` + `app/core/date_patterns.py` | The existing deterministic date-extraction module and its shared regex patterns (month-name / ISO / numeric, calendar-validated, fixed confidence per pattern family, 2-digit years never guessed). Directly reusable for extracting IEP meeting dates, service start/end dates, and any other date-typed structured field — no new date-parsing logic needs writing. Also directly demonstrates this codebase's idempotent-rescan pattern (`_observation_exists_for_span()`): re-running extraction never creates a duplicate row for a span already observed. §7's flag-idempotency design reuses this exact idea. |
| `TimelineEvent`/`TimelineEventFact` + `app/core/timeline/service.py` | Existing timeline built strictly from `verified_facts`. Reused only as an optional *link target* (the mockup's "Link to timeline/fact if appropriate" action) — a confirmed inconsistency flag can point at an existing timeline event or verified fact for context; nothing here writes new timeline events automatically. |
| `Communication`/`CommunicationAttachment`/`CommunicationDocumentLink` + `app/core/communications/*` | The Communications phase's structured email model (parsed sender/subject/dates/body, thread reconstruction, attachment→Document promotion with cross-email duplicate-Document detection). Reused for "relevant Communication/email → IEP where structured evidence supports comparison" (§4, across-document rules) — an email is a legitimate comparison source, but (like `AiObservation.communication_id`) it has no page/offset citation concept, which this plan's schema has to account for explicitly (§2, §6). |
| `app/core/annotations/service.py::create_highlight()` | The exact pattern for "a human selects a span of a page's effective text, and a `Citation` gets created from it." Directly reused for the proposed manual/assisted structured-field entry path (§3) — creating an `iep_records`/`iep_record_fields` row by hand uses the identical citation-creation mechanics as creating a highlight. |
| `app/core/indexing/search.py` (FTS5 pattern) | Not reused directly for v1 (this feature doesn't need full-text search over structured fields, only filtered listing), but the exact precedent (`document_text_fts`, external-content, trigger-synced, with a documented historical gotcha about Alembic autogenerate misreading FTS5 shadow tables — see `tests/test_migrations_no_fts5_drops.py`) is the template if a future step adds full-text search over extracted IEP field text. |
| `docs/PRIVACY_SECURITY.md` §9 ("Language/UI discipline") and §10 ("AI inference boundary") | The *exact* legal-boundary rule this feature must follow is already written down and already enforced elsewhere in the app (neutral gap/conflict language; machine output is never silently promoted to "established"). This plan does not need to invent that rule — only apply it, and the fixed-string mechanism `AiSummary.label_text` already demonstrates ("a fixed, app-enforced constant... rendered wherever it appears... regardless of review status") is the direct template for how "Possible inconsistency" language gets enforced in code, not just in a UI copy guideline. |
| Migration/testing conventions established across every phase this session | Additive-only migrations (no `ALTER` on existing evidentiary tables unless truly required), Alembic + hand-verification against the FTS5-drop gotcha, `db_session`/`sample_case`/`vault` pytest fixtures, per-step discipline (implement → targeted tests → full suite → FTS5 guard → backward-compat stash check → live smoke test → commit → pause for review). §12 below follows this exactly. |

## 2. Proposed schema

### 2.1 Design decision: hybrid, not a reuse of `verified_facts`/`ai_observations`

`verified_facts`/`ai_observations` are **atomic, single-statement, single-date,
case-scoped claims** ("IEP annual review meeting held", "Possible date:
2024-03-12") with a citation set. They were built for exactly that shape and
are used correctly, everywhere, for that shape. An IEP service entry is not
that shape — it's a small *record* with several independently comparable
typed fields (service name, provider, minutes, frequency count, frequency
period, location, start date, end date). Forcing that into
`verified_facts.statement` as free text (e.g. "Speech-language therapy — 30
minutes, 2x/week, resource room") would satisfy the letter of "everything
has a citation" while making the actual comparisons this feature exists to
do — is 30 ≠ 20, is "2x/week" ≠ "1x/week" — dependent on re-parsing that
string later, which is exactly the brittleness the requirements explicitly
warn against.

The proposed shape is a **hybrid**, and it deliberately follows a shape
*already used twice elsewhere in this schema* rather than inventing a new
pattern: a lookup table for "what kind of thing is this" (matching
`document_types`/`fact_types`/`event_types`'s "row insert, not a migration"
extensibility), a lookup table for "what kind of field is this" (same
extensibility idea, one level down), an identity/provenance row per
extracted record, and narrow, strongly-typed value rows per field — the same
"one identity row, one or more typed detail rows referencing it" shape
`Citation`+`Annotation`, or `DocumentPage`+`OcrCorrection`, already use.

### 2.2 New lookup tables (all additive, all extensible-by-row-insert)

**`iep_record_types`** — what kind of extracted structural unit this is.
- `type_id` (PK), `name` (unique), `description`, `is_active`
- Seeded: `service`, `goal`, `accommodation`, `modification`,
  `assistive_technology`, `transportation_support`, `present_level_need`,
  `disability_eligibility`, `evaluation_finding`, `pwn_decision`,
  `parent_concern`. (A new record type later — e.g. "related_service_log"
  — is an `INSERT`, exactly like adding a new `document_types` row today.)

**`iep_field_types`** — what kind of individual value this is, plus how to
compare it.
- `type_id` (PK), `name` (unique), `value_kind` (`text` / `number` / `date`),
  `description`, `is_active`
- Seeded (illustrative, not exhaustive — every one of these is a plain row,
  addable later without a migration): `service_name`, `provider`, `minutes`,
  `frequency_count`, `frequency_period`, `duration_weeks`, `location`,
  `start_date`, `end_date`, `goal_area`, `goal_identifier`, `baseline_text`,
  `target_text`, `accommodation_text`, `modification_text`,
  `disability_category`, `eligibility_status`, `finding_text`,
  `decision_text`, `concern_text`, `assistive_technology_text`,
  `transportation_text`, `iep_meeting_date`, `iep_effective_start_date`,
  `iep_effective_end_date`.

**`iep_inconsistency_types`** — what kind of flag this is (drives the
fixed, neutral `reason_text` template — see §9).
- `type_id` (PK), `name` (unique), `description`, `is_active`
- Seeded: `service_minutes_mismatch`, `service_frequency_mismatch`,
  `service_location_mismatch`, `service_provider_mismatch`,
  `duplicate_record_conflicting_field`, `goal_missing_for_need`,
  `goal_missing_baseline`, `goal_missing_target`, `accommodation_added`,
  `accommodation_removed`, `date_conflict`, `eligibility_mismatch`,
  `pwn_iep_mismatch`, `field_changed_between_versions`.

**`iep_document_link_types`** — the version/relationship vocabulary (§5).
- `type_id` (PK), `name` (unique), `description`, `is_active`
- Seeded: `prior_iep_to_current_iep`, `annual_iep_to_amendment`,
  `amendment_to_final_iep`, `evaluation_for`, `eligibility_determination_for`,
  `pwn_for`, `progress_report_for`, `related_communication`.

### 2.3 Structured-extraction tables

**`iep_records`** — one row per logical extracted unit within one source
(a service line, a goal, an accommodation, ...). One real-world "service"
mentioned in two different places in the same IEP (a services table on
page 4 *and* a summary table on page 9) is **two separate rows** — that's
what makes "duplicated section with different values" and "summary vs.
detail mismatch" comparable at all, rather than silently merged into one.

- `record_id` (PK)
- `case_id` (FK → `cases`, denormalized for case-scoped queries — same
  convenience denormalization `AiObservation.case_id` already uses)
- `document_id` (FK → `documents`, **nullable**)
- `communication_id` (FK → `communications`, **nullable**) — exactly one of
  `document_id`/`communication_id` is set, validated at the application
  layer in the same transaction as the write, mirroring
  `AiObservation.communication_id`'s exact precedent for "a Communication
  has no page/offset citation concept, so it needs its own direct anchor
  alongside the Document-shaped path."
- `record_type_id` (FK → `iep_record_types`)
- `section_label` (text, e.g. `"Services table, page 4"` / `"Summary page"`
  — human-readable context, never itself compared)
- `comparison_key` (text, nullable) — a deterministically normalized
  identity string (lowercased, whitespace-collapsed) used to match "the
  same real-world thing" across records for comparison purposes (e.g. two
  service records both normalizing to `"speech-language therapy"`).
  **Never a fuzzy/similarity score** — either the normalized strings match
  exactly, or the two records are never paired for comparison at all. A
  real match the normalizer misses simply produces no flag (silence, not a
  wrong flag) — see §4's "never guess" discipline.
- `status` — `active` (default) / `excluded`. The one escape hatch a human
  has over a specific extraction without a full correction UI: "this
  extraction is wrong or garbage, stop comparing it." Excluding a record
  does not delete it or its fields (append-only spirit, matching
  `deleted_at` soft-delete elsewhere) and does not affect any flag already
  raised from it (see `extracted_value_a`/`extracted_value_b` below).
- `extraction_method` (text, e.g. `"iep-service-line-regex-v1"`,
  `"manual"` — same explainable/versioned convention as
  `AiObservation.method`)
- `extracted_at`, `created_by`

**`iep_record_fields`** — the individual typed values belonging to one record.

- `field_id` (PK)
- `record_id` (FK → `iep_records`)
- `field_type_id` (FK → `iep_field_types`)
- `text_value` (nullable)
- `numeric_value` (nullable, Float)
- `date_value` (nullable, DateTime)
- `unit` (nullable text — e.g. `"week"`/`"month"`/`"session"` for a
  `frequency_period` field; most field types imply their own unit and
  leave this null)
- `citation_id` (FK → `citations`, **nullable** — null only when the
  parent record's `communication_id` is set, since a Communication has no
  citable page/offset span; required whenever `document_id` is set,
  enforced at the application layer exactly like every other
  citation-requiring write in this app)
- `extraction_confidence` (nullable Float — populated only when the
  extractor itself produces a confidence, e.g. from OCR text quality via
  `effective_text()`'s existing confidence; **never used to suppress or
  soften a flag** — a field with low confidence either got extracted
  (and is compared normally) or didn't (and produces no field row at
  all), matching `document_type_suggestion.py`'s "ambiguous → return
  nothing" discipline rather than a fuzzy partial-confidence flag)

**`iep_document_links`** — the narrowly-scoped, IEP-specific equivalent of
the relationship graph ARCHITECTURE.md §3.9 describes but that was never
built (§0). Mirrors the exact verified/suggested split already established
for facts, applied to "these two sources relate to each other."

- `link_id` (PK)
- `case_id` (FK → `cases`)
- `link_type_id` (FK → `iep_document_link_types`)
- `from_document_id` (FK → `documents`)
- `to_document_id` (FK → `documents`, nullable)
- `to_communication_id` (FK → `communications`, nullable) — exactly one of
  `to_document_id`/`to_communication_id` set, same pattern as `iep_records`
- `status` — `pending_review` (auto-suggested, not yet acted on) /
  `confirmed` (human-confirmed, or created directly by a human) /
  `dismissed` (human said "no, these aren't related")
- `method` (text, e.g. `"manual"` / `"same-case-chronological-iep-heuristic-v1"`)
- `created_by`, `created_at`, `reviewed_by` (nullable), `reviewed_at` (nullable)

**Only `confirmed` links are ever used by the across-document comparison
engine in §4.** A `pending_review` suggestion is shown to the user for
confirmation and never silently treated as fact — this is what satisfies
"do not silently treat semantic similarity as fact" structurally, not just
by convention: there is no code path from a suggested link to a
comparison run.

### 2.4 The flag table

**`iep_inconsistency_flags`** — the actual review-lifecycle object the
mockup describes. This is the schema-level answer to §7's requirements in
full: "preserve detection rule, source A, source B, original extracted
values, user decision, optional note, timestamp."

- `flag_id` (PK)
- `case_id` (FK → `cases`, denormalized — this is the primary review-queue
  object, same reasoning as `AiObservation.case_id`)
- `inconsistency_type_id` (FK → `iep_inconsistency_types`)
- `comparison_mode` — `within_document` / `across_document`
- `source_a_record_id` (FK → `iep_records`)
- `source_a_field_id` (FK → `iep_record_fields`, nullable — set for a
  single-field mismatch like minutes; null for a whole-record-level flag
  like "duplicate service entry, multiple fields differ")
- `source_b_record_id` (FK → `iep_records`)
- `source_b_field_id` (FK → `iep_record_fields`, nullable)
- `rule_id` (text, e.g. `"service_schedule_mismatch_v1"` — the specific
  versioned deterministic rule that produced this flag; §4)
- `reason_text` — **not free text**. Composed by the rule from a small set
  of fixed templates (see §9), the same "fixed, app-enforced constant"
  discipline `AiSummary.label_text` already uses, so no code path can ever
  render conclusory language here.
- `extracted_value_a` / `extracted_value_b` (JSON) — a snapshot of exactly
  what was compared at flag-creation time (text/numeric/date/unit values,
  section label, document identity), independent of whatever
  `iep_record_fields` might show later. This is the same "record what was
  true at this event, not just a pointer to something that could change
  meaning later" discipline `DocumentCustodyEvent`'s `sha256_hash_at_event`
  already uses, applied here because §7 explicitly requires the flag to
  preserve "original extracted values" even if the underlying record is
  later `excluded`.
- `status` — `pending` / `confirmed` / `dismissed` (exactly the three
  states §7 asks for)
- `user_note` (nullable text)
- `reviewed_by` (nullable), `reviewed_at` (nullable)
- `linked_timeline_event_id` (FK → `timeline_events`, nullable) — the
  mockup's "Link to timeline/fact if appropriate" action
- `linked_verified_fact_id` (FK → `verified_facts`, nullable) — same
  action, when linking to a fact rather than a full timeline event is
  the better fit
- `dedup_key` (text, unique together with `case_id` — a deterministic hash
  of `(inconsistency_type_id, source_a_record_id, source_a_field_id,
  source_b_record_id, source_b_field_id, rule_id)`) — the mechanism behind
  §7's idempotency requirement (full explanation there)
- `created_at`

No separate flag-event ledger table is needed: every status transition
(confirm/dismiss/note) is also written to the existing case-scoped
`audit_log`, exactly the way `create_verified_fact()`/`promote_observation()`/
`reject_observation()` already log every state change there today — full
history without a new append-only table.

### 2.5 Why not one wide table, and why not pure EAV

Two alternatives were considered and rejected:

- **One wide table** (`iep_data_points` with a nullable column per possible
  field across every record type) would avoid the field-type lookup table,
  but produces an extremely sparse table (a `goal` row would have a dozen
  null columns meant for `service` fields) and — critically — makes adding
  a new field type later a real migration again, which is exactly the
  extensibility property this schema (like every lookup-table pattern
  already in this codebase) is designed to avoid.
- **Pure EAV** (one big `key`/`value` text table, similar to the existing
  `document_metadata` table's shape) avoids new lookup tables entirely but
  reintroduces the brittleness this plan is explicitly trying to avoid:
  `document_metadata` works because it holds rarely-needed, rarely-compared
  per-type attributes, not fields whose entire purpose is being reliably
  compared as numbers, dates, and normalized text. A `minutes` field stored
  as a bare string is exactly the "stuffing complex structured IEP data
  into free-form fact text" the requirements warn against, one layer down.

The proposed design (typed value columns — `text_value`/`numeric_value`/
`date_value` — on a per-field-type row, extensible by inserting new
`iep_field_types` rows, never by migration) gets the extensibility of EAV
without the brittleness of storing everything as strings.

## 3. Extraction architecture

### 3.1 Where extraction runs, and what it reads

New extractors live in `app/core/iep_extraction/` (new package), one module
per record type (`services.py`, `goals.py`, `accommodations.py`, `dates.py`,
`eligibility.py`), each exposing a single `extract_<type>_records(db, case,
document, actor) -> list[IepRecord]` function with the exact same shape and
transaction discipline as `extract_date_observations()`
(`app/core/facts/date_extraction.py`): reads `document.pages`, resolves each
page through `effective_text()` (never `page.extracted_text`/`page.ocr_text`
directly), never commits (caller controls the transaction), and is
idempotent by construction (checks for an existing `iep_records` row citing
the same page+span before creating a new one — the exact
`_observation_exists_for_span()` pattern).

A parallel `app/core/iep_extraction/communications.py` runs the same
service/date extractors against `Communication.body_text`, producing
`iep_records` rows with `communication_id` set and `document_id`/citation
null on the field rows — the mechanism behind "relevant Communication/email
→ IEP where structured evidence supports comparison" (§1, §2.3).

Extraction is triggered **manually**, not automatically on every document
ingest — a "Scan for structured data" action on a document's detail page
(and a "Scan all IEP-family documents" action at the case level). This is a
deliberate choice, not an oversight: automatic extraction on every ingest
would run regex extractors against every document type, including ones
where they'd never match anything (a Report Card, a Medical record), for no
benefit — matching this app's existing pattern of extraction being
triggered by ingestion but *review* (fact promotion, OCR correction) always
being an explicit user action.

### 3.2 What's realistically deterministic to extract, and what isn't

This is the highest-risk part of the whole feature, and worth being
explicit about rather than optimistic. District IEP templates vary
enormously — there is no universal PDF layout. The extractors below are
ordered by how regular their source text tends to be across templates, which
is also the proposed build order (§12):

1. **Services** (highest confidence). Most IEP service tables render as a
   recognizable line/row pattern even after PDF-to-text extraction loses
   visual table structure — a service name, followed within a short
   character window by a number and "minute(s)", followed by a
   frequency phrase ("2x/week", "2 times per week", "twice weekly"),
   often followed by a location. A fixed-pattern-family regex approach
   (mirroring `app/core/date_patterns.py`'s "three fixed pattern families,
   each with its own confidence" shape) can extract `service_name`,
   `minutes`, `frequency_count`, `frequency_period`, and (best-effort)
   `location`/`provider` from a matched line. **When a line doesn't
   cleanly match one of the fixed patterns, no record is created** — the
   same "ambiguous → nothing, never a guess" rule
   `document_type_suggestion.py` already follows.
2. **Dates** (already solved). The IEP meeting date, effective start/end
   dates — reuses `app/core/date_patterns.py` directly, scoped to text
   windows near a recognizable label ("Meeting Date:", "Effective:",
   "IEP Duration:").
3. **Accommodations/Modifications** (moderate confidence). Usually
   list-formatted ("Accommodations: 1. ... 2. ... 3. ..." or bullet runs)
   — extractable as a list of normalized text items, compared by set
   membership (added/removed) rather than sub-field comparison, since an
   accommodation doesn't really have separately-comparable sub-attributes
   the way a service does.
4. **Goals** (harder, and scoped down honestly). Full goal narrative
   (baseline description, measurable target) is free prose with no
   reliable universal structure. **v1 extracts presence/absence and a
   `goal_area` label only** (e.g. "Reading Fluency", detected via a
   heading-style trigger similar to `document_type_suggestion.py`'s
   trigger list), plus presence/absence of a `baseline_text`/`target_text`
   block near that heading — enough to support "is there a goal for this
   area at all" and "does this goal have both a baseline and a target
   present" (§4 rules #4 and #12), but **not** a claim that the extracted
   baseline/target text is itself compared field-by-field between
   versions — that's a narrative-comparison problem, addressed honestly
   in §4/§9 as out of deterministic scope.
5. **Eligibility/disability category** (label-level only). Most IEPs and
   Evaluations state this as a short, fairly standardized label ("Specific
   Learning Disability", "Other Health Impairment", ...) near a
   recognizable heading — extractable and comparable as normalized text,
   the same way `service_name` is.
6. **PWN decisions, evaluation findings, parent concerns** (narrowest
   deterministic value; see §4/§9). These sections are the most
   narrative of all. v1 extracts them only where they happen to restate
   something already structured elsewhere (a PWN that literally restates
   a proposed service line uses the *same* services extractor as #1; an
   evaluation's stated eligibility uses the *same* extractor as #5).
   Structural presence/absence of a labeled section (e.g. "Parent
   Concerns:") is extractable and useful as a coverage indicator (§4 rule
   discussion), but **content-level comparison of two narrative passages
   is explicitly out of deterministic scope** — see §4.11 and §9.

### 3.3 Manual/assisted entry as the honest fallback

Because deterministic extraction quality will vary a lot by district
template — and because the feature should have real value even where it
doesn't — a manual entry path is proposed alongside every automatic
extractor, not as a v2 afterthought: a small form on a document's detail
page lets a user select a span of the (already-rendered) page text and fill
in the structured fields for one `iep_records` row by hand, reusing
`create_highlight()`'s exact citation-creation mechanics (a `Citation` row
is created from the selected span, same as a highlight). This is the same
relationship `document_type_suggestion.py` has to `set_document_type()`:
the deterministic path is a suggestion/accelerator, the human always has a
direct, equally-first-class way to assert the same information — nothing in
the comparison engine (§4) distinguishes "automatically extracted" from
"manually entered" `iep_records` rows; only `extraction_method` records
which one it was, for transparency.

## 4. Deterministic comparison rules

All comparisons are exact, not fuzzy. No comparison here computes a
similarity score or a "probably the same" judgment on the *value* being
compared (only `comparison_key` matching, itself exact-after-normalization,
decides which records get compared at all — see §2.3). No numeric
tolerance is built in (a 25-vs-30-minute difference always flags; whether
that's a meaningful discrepancy or an intentional rounding is exactly the
judgment a human makes via "Mark not an inconsistency," not something the
tool decides for them by silently allowing a margin).

**Comparison primitives**, keyed by `iep_field_types.value_kind`:
- `text` — normalized-equality (lowercase, collapsed whitespace, stripped
  punctuation). Used both for `comparison_key` matching (decides *whether*
  two records are compared) and, separately, for flagging when a
  non-identity text field (e.g. `location`) differs between two records
  already matched by `comparison_key`.
- `number` — exact equality after parsing to float.
- `date` — exact calendar-date equality (day precision; time-of-day is
  never compared).
- presence/absence — used where the comparison is "does a corresponding
  record/field exist at all," not a value comparison.

**Within-document rules** (`comparison_mode = within_document`; both sides
are `iep_records` from the same `document_id`):

1. **`service_schedule_mismatch`** (→ `service_minutes_mismatch` /
   `service_frequency_mismatch`, one flag per differing dimension). Two
   `service`-type records in the same document whose `comparison_key`
   matches (same normalized service name) but whose `minutes` and/or
   `frequency_count`/`frequency_period` differ. This is the exact scenario
   the mockup shows ("Services section... 30 minutes, 2x/week" vs.
   "Summary section... 20 minutes, 1x/week").
2. **`service_location_mismatch`** / **`service_provider_mismatch`** — same
   pairing, comparing `location`/`provider` instead.
3. **`duplicate_record_conflicting_field`** — the generalized form of #1/#2,
   applied to `goal`/`accommodation` record types where a structured
   sub-field (not narrative text) differs between two same-`comparison_key`
   records in the same document (e.g. two "Reading Fluency" goal records
   whose extracted target dates or numeric targets differ). This is also
   the mechanism for "inconsistencies between summary pages and detailed
   service pages" — a summary-page record and a detail-page record are
   just two `iep_records` with the same `comparison_key` and different
   `section_label`.
4. **`goal_missing_for_need`** — for every `present_level_need` record (only
   extracted where a structural "Areas of Need"-style label exists — see
   §3.2's honesty note; this rule simply never fires where that structural
   cue is absent, rather than guessing from narrative Present Levels text),
   check whether a `goal` record with a matching `comparison_key`
   (normalized need/goal area) exists in the same document. No match →
   flag.
5. **`goal_missing_baseline`** / **`goal_missing_target`** — a `goal` record
   with no `baseline_text`/`target_text` field row at all. Presence/absence
   only — this deliberately does **not** attempt to judge whether an
   existing baseline/target is *measurable* (a SMART-goal quality judgment,
   explicitly out of scope for both the deterministic and any future
   AI-assisted layer — see §9).
6. **`date_conflict`** — two field rows of the same `date`-kind
   `field_type_id` (e.g. two `iep_meeting_date` extractions) within the
   same document that disagree.

**Across-document rules** (`comparison_mode = across_document`; both sides'
`iep_records` belong to different documents, or one side is a
`communication_id`-anchored record — only runs across a **confirmed**
`iep_document_links` row, never a `pending_review` one):

7. **`field_changed_between_versions`** — for a `prior_iep_to_current_iep`
   or `annual_iep_to_amendment` confirmed link, compare `service`/
   `accommodation`/`goal` records with matching `comparison_key` across the
   two documents; any differing structured field → one flag per differing
   field, using the neutral `field_changed_between_versions` type (not
   "removed"/"worsened" framing — see §9). This is deliberately the same
   rule whether the change is a genuine oversight or a correctly-documented
   amendment; distinguishing those two is exactly what "Mark not an
   inconsistency" is for, not something the rule itself judges.
8. **`accommodation_added`** / **`accommodation_removed`** — set difference
   of `accommodation`-type `comparison_key`s between two linked documents'
   `active` records.
9. **`eligibility_mismatch`** — for an `evaluation_for` or
   `eligibility_determination_for` confirmed link, compare the
   `disability_category`/`eligibility_status` text between the Evaluation/
   Eligibility Determination document and the linked IEP. Label-level exact
   comparison only (§3.2 #5).
10. **`pwn_iep_mismatch`** — for a `pwn_for` confirmed link, run the *same*
    services extractor (§3.1) against the PWN document; compare any
    resulting `service` records against the linked IEP's service records by
    `comparison_key`. Only fires when the PWN happens to restate a service
    in a structured, table/line-like form — many PWN templates do, some
    don't; when it doesn't, this rule produces nothing, which is correct
    (no false claim of "PWN and IEP match" or "don't match" when the PWN's
    prose was never actually parsed).
11. **Evaluation findings not reflected in the IEP**, **parent concern
    documented in one source but absent from another** — investigated and
    **deliberately not proposed as v1 deterministic rules**. Both require
    comparing free narrative prose for semantic equivalence/absence, which
    this plan is explicit is not a deterministic-comparison problem — see
    §9 for exactly how a future, clearly-separated AI-assisted layer could
    address these without violating the "never treat semantic similarity
    as fact" instruction.

**What "section presence/absence" means here, deliberately.** A document
with zero extracted `service`/`goal` records is *not* auto-flagged as
"IEP missing services/goals" in v1. Zero extracted records is ambiguous
between two very different situations — "this record genuinely has no
services" (a real, possibly important fact) and "the extractor's regex
didn't match this district's table format" (an extraction limitation) —
and conflating them risks exactly the kind of false, alarming claim
("possible violation") the legal boundary (§9) exists to prevent. Instead,
a document with zero extracted structured records shows a neutral,
visually distinct **extraction-coverage indicator** ("No services were
automatically detected in this document — extraction may be incomplete;
add entries manually if needed") rather than a flag — never styled or
color-coded like an inconsistency, never counted in the flag queue.

## 5. Version-matching strategy

Four sub-questions, answered against what's actually real in this schema
today (§0/§1), not the never-built relationship graph:

**Which documents are IEPs (or Evaluations, PWNs, Progress Reports).**
Already answered: `Document.document_type_id`, populated by a human
confirming (or overriding) `document_type_suggestion.py`'s existing
suggestion. No new mechanism needed — this plan's version-matching and
comparison code simply queries `documents` filtered by
`document_type.name` alongside `case_id`.

**Which IEPs belong to the same student.** Trivial and already true:
`case_id`. A "student" *is* a `Case` in this schema (see the FERChronos
branding refinement's `Case.display_name`) — there is no separate student
identity to reconcile.

**Chronological order.** `Document.document_date` (already exists, already
supports exact/approximate/range precision, already has a suggestion
pipeline — `document_date_suggestion.py` — feeding it). No new column.
Documents with no `document_date` set simply can't be auto-ordered or
auto-linked by the chronological heuristic below; they remain
manually-linkable.

**Annual IEP vs. amendment vs. draft vs. final, and evaluation/reevaluation
relationships.** This is the one area needing new structure, and the
proposal is deliberately *not* a new column on `documents` (adding a
`document_role`/`iep_kind` enum column would duplicate information the
relationship itself already encodes — a document with a confirmed outbound
`annual_iep_to_amendment` link *is*, by definition, an amendment of the
document it points at). Instead:

- **`iep_document_links`** (§2.3) is populated by a deterministic,
  chronology-based **suggestion**, always requiring human confirmation
  before it's used by any comparison rule:
  - `prior_iep_to_current_iep`: for two `IEP`-typed documents in the same
    case, ordered by `document_date`, suggest a link between each
    chronologically-adjacent pair.
  - `evaluation_for` / `eligibility_determination_for`: for an
    `Evaluation`-typed document, suggest a link to the nearest IEP
    document dated on or after it, within the same case.
  - `pwn_for` / `progress_report_for`: same nearest-subsequent-IEP
    heuristic, scoped by document type.
  - `annual_iep_to_amendment`: **not** auto-suggested from chronology
    alone (two IEP-typed documents close in date could just as easily be
    "prior IEP → current IEP" as "annual → amendment," and guessing wrong
    here actively misleads the comparison rules in §4). A human creates
    this link explicitly from either document's detail page. This is a
    deliberate, named limitation, not an oversight — see the "Confirm
    before use" framing in §2.3 again: an unconfirmed/incorrectly-typed
    suggestion is inert until a human acts on it either way, so declining
    to auto-suggest amendment relationships costs nothing in safety, only
    in guessing accurately, and guessing accurately here is genuinely hard
    without reading the document.
- **Draft vs. final** is proposed to need **no schema change at all** —
  reuse the existing `Tag` model (already case-scoped, already zero-schema-
  footprint to add a value to). An optional, separate deterministic trigger
  (same shape as `document_type_suggestion.py`, scanning for a literal
  "DRAFT" watermark-style phrase near the top of page 1) can *suggest*
  applying a "Draft" tag, but the linking/comparison logic in this plan
  never depends on that distinction — a draft is still just a `Document`
  with a type and a date; nothing here compares "draft-ness."

**Relevant Communication/email → IEP.** Handled via `iep_document_links`
rows with `to_communication_id` set instead of `to_document_id` (§2.3).
Proposed v1 suggestion heuristic is intentionally conservative: suggest a
link only when a Communication's own already-structured fields (not free
prose) give a specific match — e.g. the Communication's `sent_at`/
`received_at` date exactly matches an IEP's `document_date`, or its subject
line contains one of the same phrase triggers `document_type_suggestion.py`
already recognizes for that document type. General "this email seems
related" free-text matching is explicitly not proposed for v1 — see §9.

## 6. Provenance model

Every flag traces back to real, checkable source material by construction,
never by convention:

```
Citation (document + page + exact span)      Communication (structured fields only)
        │                                              │
        ▼                                              ▼
iep_record_fields ◄──────────────────── (either path) ─┤
        │  (citation_id, OR parent record's
        │   communication_id when Communication-sourced)
        ▼
   iep_records (record_type, section_label, comparison_key,
                document_id OR communication_id)
        │
        │  source_a_record_id / source_a_field_id
        │  source_b_record_id / source_b_field_id
        ▼
iep_inconsistency_flags ── extracted_value_a/b (snapshot, JSON)
        │
        ├── linked_timeline_event_id → TimelineEvent (optional)
        └── linked_verified_fact_id → VerifiedFact (optional)
```

This is exactly what lets the UI mockup's "Source A / Source B" block
render itself directly from the schema, with zero guessing: "IEP dated
2026-04-10, Services section/page X" is `document.document_date` +
`iep_records.section_label` + `citation.page.page_number`; the quoted
service line is `citation.quoted_text`; "View Source" opens the document
viewer scrolled to that exact citation, the same viewer/route every other
citation in this app already opens.

The `extracted_value_a`/`extracted_value_b` JSON snapshot on the flag
itself (§2.4) is a deliberate second layer of provenance, independent of
the live `iep_record_fields` rows: if a record is later marked `excluded`,
or a future improved extractor version re-runs and produces a different
`iep_record_fields` value for the same span, an already-created flag's
history of "what was actually compared, and when" stays exactly as it was
— matching `document_custody_events`' snapshot-per-event philosophy, not a
live pointer that could silently change meaning underneath a human's past
decision.

## 7. Review workflow

**Lifecycle** (exactly the three states requested): `pending` →
`confirmed` / `dismissed`. Both transitions are reversible (a user can
reconsider), and every transition — including a reversal — is logged to
`audit_log`, giving a full history without a dedicated ledger table (§2.4).

**Idempotent re-run, precisely.** `dedup_key` (§2.4) is a deterministic
hash of the comparison's identity: which rule, which two records/fields.
Re-running the scan:
1. Recomputes the same candidate comparisons deterministically (same
   inputs, since documents are immutable, always produces the same
   `iep_records`/`iep_record_fields` — extraction is itself idempotent per
   §3.1 — and therefore the same `dedup_key`s).
2. For a `dedup_key` that already exists: **does nothing** — a `pending`
   flag stays `pending` (not duplicated), a `confirmed`/`dismissed` flag
   stays exactly as the human left it. This is the literal requirement:
   "must not recreate dismissed/confirmed flags as fresh pending
   duplicates unless the underlying source/version actually changed."
3. "The underlying source/version actually changed" is handled correctly
   by construction, not as a special case: if a document's structured data
   changes (only possible via re-extraction after fixing an extractor, or
   a human editing/adding a manual `iep_records` entry), the resulting
   `iep_records`/`iep_record_fields` identity changes, which changes the
   `dedup_key` for any comparison involving it — producing a genuinely new
   flag rather than colliding with the old one, which correctly stays
   exactly as it was (still referencing its own `extracted_value_a/b`
   snapshot of the old data).
4. A comparison whose `dedup_key` no longer occurs at all (e.g. a record
   was `excluded`, or an `iep_document_links` row was `dismissed`) leaves
   its existing flag untouched too — dismissing the underlying record/link
   is itself a reviewable action (logged), and silently deleting a flag
   that a human may have already acted on would destroy exactly the
   history §7 requires preserving. A `pending` flag whose source became
   `excluded`/`dismissed` is instead visually marked "source no longer
   active" in the queue, still fully inspectable, still actionable.

**UI actions**, matching the mockup exactly: Confirm inconsistency / Mark
not an inconsistency / Add note / Link to timeline or fact / View both
sources. All five map directly to columns already in §2.4's schema.

## 8. UI proposal

- **Case-level "Consistency Review" page** (new route, same tab-bar level
  as the existing Search/Timeline/Facts Review pages) — a flag queue with
  filters mirroring `facts_review.html`'s existing conventions: by
  `inconsistency_type`, `status`, `comparison_mode`, and by which
  document(s) are involved. A prominent, explicit "Scan for
  Inconsistencies" action (not automatic — see §3.1) with a plain summary
  of what it will do (which documents, which confirmed links) before
  running.
- **Flag card**, rendering exactly the mockup's shape:
  ```
  Possible service inconsistency
  Source A: IEP dated 2026-04-10, Services section/page X
            Speech-language therapy — 30 minutes, 2x/week
  Source B: Same IEP, Summary section/page Y
            Speech-language therapy — 20 minutes, 1x/week
  Reason flagged: Frequency and duration do not match.
  [Confirm inconsistency] [Mark not an inconsistency] [Add note]
  [Link to timeline/fact] [View Source A] [View Source B]
  ```
- **Document detail page** gains an "Extracted Structured Data" panel
  (only shown for IEP-family document types) listing that document's
  `iep_records` grouped by `record_type`, each with its citation link and
  an `excluded`/`active` toggle, plus a "+ Add entry manually" action
  (§3.3).
- **Document relationships panel**, also on IEP-family document detail
  pages: lists `pending_review` suggested `iep_document_links` (with
  Confirm/Dismiss buttons) and `confirmed` ones (read-only, with an
  "unlink" affordance), plus a "Link another document/email..." manual
  action.
- **Extraction-coverage indicator** (§4): a distinct, non-flag visual
  treatment ("Extraction may be incomplete") wherever a document of an
  IEP-family type has zero extracted records of an expected type —
  visually and semantically separate from the flag queue so it is never
  mistaken for a substantive finding.

## 9. Privacy/security considerations

- **Fully local, fully deterministic for every rule in §4.** No network
  call, no LLM, no cloud service, anywhere in this feature as proposed —
  consistent with the standing rule in `docs/PRIVACY_SECURITY.md` and with
  every deterministic module already in this codebase
  (`document_type_suggestion.py`, `date_patterns.py`).
- **New evidentiary surface, same protection level as existing evidentiary
  data.** `iep_records`/`iep_record_fields`/`iep_inconsistency_flags` live
  in the same unencrypted SQLite `db.sqlite` as everything else
  (`docs/SECURITY_ENCRYPTION_AT_REST.md`'s status is unchanged by this
  plan — no new decision needed here, no new risk introduced beyond what
  already exists for `documents`/`verified_facts`).
- **The legal-boundary rule is enforced in code, not just in this
  document.** `reason_text` (§2.4) is composed from a small, fixed set of
  neutral templates per `iep_inconsistency_types` row — e.g. "Frequency
  and duration do not match," "A value changed between these two versions,"
  "No corresponding goal was found for this identified need" — **never**
  user-typed at generation time and **never** containing "violation,"
  "illegal," "noncompliant," "denial of FAPE," or any other conclusory
  legal term, matching `AiSummary.label_text`'s existing "fixed,
  app-enforced constant" mechanism exactly. `user_note` (free text) is the
  only free-text field anywhere in this schema, and it's explicitly the
  human's own words, not the app's — no different from a note a user
  already attaches to a document today.
- **"Possible," never "confirmed," until a human says so.** A flag's
  default and only-ever-machine-set status is `pending`; nothing in this
  plan ever auto-sets a flag to `confirmed`. `confirmed` still doesn't
  mean "FERChronos asserts this is a real problem" — it means "a human
  looked at both sources and agrees they're inconsistent," which is
  exactly what the mockup's button says.
- **A future AI-assisted / semantic layer, proposed separately, as
  instructed.** Sections 4.11 and 5's Communication-linking discussion
  both identify real gaps a deterministic approach can't close: narrative
  evaluation-finding-vs-IEP comparison, PWN prose beyond restated service
  lines, parent-concern narrative matching across a Communication and an
  IEP, and general (non-date-anchored) email-to-IEP relevance. If pursued
  later, per the explicit instruction, it must be a **structurally
  separate** layer, following the *exact* pattern already established
  twice in this codebase (`ai_observations` vs. `verified_facts`;
  `ai_suggested_relationships` vs. `verified_relationships`):
  - a new `iep_semantic_suggestions` table (not proposed for
    implementation now — sketched here only so the separation is
    concrete): machine output, its own `method`/`status`/review fields,
    **never** read by the flag-review queue as if it were a deterministic
    flag, requiring the identical `pending_review`/`confirmed`/`rejected`
    human gate before it could ever produce something resembling an
    `iep_inconsistency_flags` row (and even then, via an explicit
    promotion step that creates a new flag row with lineage back to the
    suggestion — never by the suggestion silently becoming a flag).
  - must retain exact source citations, exactly like every other
    machine-suggested row in this app.
  - must never overwrite `iep_record_fields`/`iep_records` — a semantic
    suggestion is additive, informational, and reviewable, never a
    correction applied automatically to extracted data.
  - must never itself contain or imply a legal conclusion — the same
    fixed-template discipline as §9's deterministic `reason_text`, not
    free-form model output rendered directly to the user.
  - This is explicitly **not** part of the implementation sequence in
    §12 — it's recorded here so a future decision to build it starts from
    an already-reviewed shape rather than improvising the boundary again.

## 10. Migration/backward-compatibility plan

**Every table in §2 is new. Nothing existing is altered.** No column is
added to `documents`, `citations`, `verified_facts`, `communications`, or
any other existing table — confirmed against §5's own reasoning (version
role is expressed via the new `iep_document_links` table, not a new column
on `documents`; see §5's explicit rejection of that alternative).

This means:
- A single additive Alembic migration (or a small additive sequence,
  following this repo's precedent of one migration per schema slice — see
  `eb9a3291e2e9_add_communications_schema.py` for the most recent
  comparable-sized precedent) creates the eight new tables in §2 and seeds
  the four lookup tables via `app/db/seed.py`, following the exact
  `seed_document_types()`/`seed_fact_types()`/`seed_event_types()`
  precedent (idempotent, safe to re-run, called from `main.py` startup).
- **No FTS5 tables are proposed** in this plan (§1 notes the search
  precedent only as a template for a *possible future* addition), so the
  historical Alembic-autogenerate-misreads-FTS5-shadow-tables gotcha
  (`tests/test_migrations_no_fts5_drops.py`) doesn't apply to this
  migration at all — worth confirming explicitly in review rather than
  assuming, but the schema as designed has no virtual tables to trip it.
- Existing rows in every existing table are completely unaffected — a
  vault with real case data upgrades by running the new migration and
  gains zero behavior until a user explicitly runs "Scan for Inconsistencies"
  or manually adds an `iep_records` entry. This matches every other
  phase's backward-compatibility discipline in this session (Communications
  Steps 9-12 in particular, each verified via a `git stash -u` +
  baseline-suite + `git stash pop` check) and should be verified the same
  way here.
- No change to any existing API route, template, or core module's public
  signature is required to add this schema — everything is additive new
  routes/templates/modules, same "backward compatibility by construction"
  approach used for every Communications-phase step.

## 11. Testing strategy

Mapped directly to the scenarios requested, plus the additional coverage
this plan's specific design choices need. Every test uses the existing
`db_session`/`sample_case`/`vault` pytest fixtures and, where HTTP-level,
the existing `client`/`app` fixtures — no new test infrastructure needed
(there is no fake-transport equivalent to build; everything here is local
extraction and comparison over data already in the test database).

**Extraction (`app/core/iep_extraction/`)**
- Service line extraction matches a clean, well-formatted line; produces
  no record for an unmatched/ambiguous line (never a guess).
- Re-running extraction on an unchanged document produces zero new records
  (idempotency, mirroring `_observation_exists_for_span()`'s test coverage).
- A manually-created `iep_records` entry (via the highlight-style flow)
  produces an identical shape to an automatically-extracted one for
  comparison purposes.
- Communication-sourced extraction produces a record with `citation_id`
  null and `communication_id` set; a Document-sourced record never has
  both.

**Deterministic comparison (`app/core/iep_consistency/rules.py`)**
- Same-document conflicting services (the mockup scenario exactly) →
  `service_minutes_mismatch` and `service_frequency_mismatch` flags.
- Identical services (same normalized name, same minutes/frequency/
  location) in two sections → **no** flag.
- Changed service between two chronologically-linked annual IEPs (a
  confirmed `prior_iep_to_current_iep` link) → `field_changed_between_versions`.
- An intentional, amendment-documented change (a confirmed
  `annual_iep_to_amendment` link with a genuinely different service
  schedule) → flag is still raised (the rule doesn't distinguish
  "intentional"), and dismissing it via "Mark not an inconsistency"
  correctly leaves it `dismissed`, not silently suppressed at generation
  time — proving the tool never pre-judges intent for the user.
- Missing goal for an identified need (only when the structural "Areas of
  Need" cue is present) → `goal_missing_for_need`; the same document
  *without* that structural cue → no flag (never a guess from prose).
- Evaluation finding (disability/eligibility label) not matching the
  linked IEP's own label → `eligibility_mismatch`; a matching label → no
  flag.
- PWN/IEP mismatch when the PWN restates a service line differently than
  the IEP → `pwn_iep_mismatch`; a PWN with pure narrative (no matchable
  service line) → no flag, not a false "match."
- Date conflict within one document (two `iep_meeting_date` extractions
  disagreeing) → `date_conflict`.
- Accommodation removed between versions → `accommodation_removed`; an
  accommodation present in both → no flag for it.
- Ambiguous extraction (ORed pattern families / low-signal text) never
  produces a forced/guessed comparison — asserted directly against the
  extractor (no record created) rather than indirectly through the
  comparison layer.

**Idempotency / review lifecycle (`app/core/iep_consistency/service.py`)**
- Running the scan twice with no underlying change produces the same flag
  set, no duplicates (`dedup_key` collision correctly no-ops).
- A `dismissed` flag stays `dismissed` after a re-scan; a `confirmed` flag
  stays `confirmed` after a re-scan.
- A flag whose underlying `iep_records` row is later `excluded` retains its
  original `extracted_value_a`/`extracted_value_b` snapshot unchanged.
- A genuinely changed underlying value (simulated by adding a corrected
  manual `iep_records` entry) produces a **new** flag with a new
  `dedup_key`, not a mutation of the old one.

**Provenance / integrity**
- Every flag's `source_a`/`source_b` resolve to a real `Citation` (or
  `communication_id`) whose `quoted_text`/document/page match what the
  flag displays — exact round-trip, no reconstruction from filenames or
  free text.
- Underlying `Document`/`Communication` originals (bytes, hash, read-only
  flag) are unchanged after a scan runs — the extraction/comparison layer
  never opens a source file in write mode (same guarantee every extractor
  in this app already has, verified the same way `tests/test_extraction_service.py`
  verifies it for the existing extractors).
- `iep_document_links` suggestions never get used by the comparison engine
  while `pending_review` — a direct test asserting a rule produces zero
  flags across a pair of documents with only an unconfirmed suggested link
  between them, and the expected flags appear the moment that link is
  confirmed.

**Regression** (per this session's established discipline, §12): targeted
tests for each new module, full existing suite (currently 1146 passed, 1
skipped as of Communications Step 12), FTS5 guard (no new virtual tables,
but the guard test itself should still pass untouched), backward-
compatibility stash check, and a live smoke test once UI routes exist.

## 12. Staged implementation sequence

Following this session's established per-step discipline exactly
(implement → targeted tests → full regression suite → FTS5 guard →
backward-compatibility stash check → live smoke test → direct DB/
filesystem integrity verification → scope-creep review → commit → push →
**stop for explicit approval before the next step**) — proposed only for
after this plan itself is approved:

1. **Schema only.** The four lookup tables + `iep_records` +
   `iep_record_fields` + `iep_document_links` + `iep_inconsistency_flags`
   (§2), one additive migration, seed data. No extraction, no comparison,
   no UI, no behavior — matching Communications Step 1's own "schema
   migration + models + vault layout addition, no behavior, no UI"
   precedent exactly.
2. **Services extractor** (§3.2 #1, the highest-confidence, highest-value
   extractor, and the one the mockup itself is built around) + the manual/
   assisted entry path (§3.3), sharing one core module and one document-
   detail-page panel.
3. **Within-document comparison engine + flag review UI** (§4 rules #1-#3,
   §7, §8's flag card and Consistency Review page) — the smallest slice
   that delivers the mockup's exact end-to-end experience: extract two
   conflicting services from one document, get a flag, confirm/dismiss it.
4. **Version-matching** (`iep_document_links` suggestion + confirm UI,
   §5) + **across-document service rules** (§4 rules #7-#8, prior→current
   IEP and PWN→IEP).
5. **Goals** (§3.2 #4, presence/area/baseline/target-presence extraction) +
   goal-related rules (§4 rules #4-#5, plus the across-document "goal
   added/removed/changed" extension of rule #7).
6. **Accommodations/modifications** extraction + added/removed rules
   (§4 rule #8).
7. **Dates + eligibility/disability** extraction and their rules (§4 rules
   #6, #9).
8. **Communication-linking** (§5's conservative heuristic) +
   extraction-coverage indicator UI polish (§4's "not a flag" treatment,
   §8).
9. **Docs, full-phase verification, and an explicit scope note** that the
   semantic/AI-assisted layer (§9) remains unbuilt and ungated behind any
   future, separately-approved decision — matching this plan's own
   instruction not to build it now.

Each step above is sized to match this session's established per-step
review cadence — implement, verify, commit, **stop for approval** — not to
be run together. Step 1 is the natural first step once this plan itself is
approved.
