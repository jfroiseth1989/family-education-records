# Phase 4 Implementation Plan — Timeline

Status: **Approved for implementation.** Scope confirmed against
`docs/PROJECT_PLAN.md` Phase 4, `docs/ARCHITECTURE.md` §3.8, and
`docs/DATA_MODEL.md`'s `timeline_events`/`timeline_event_facts` schema.
Four open decisions were resolved before this plan was written (see §1).
No code has been written yet as of this document.

## 1. Decisions locked in

1. **Event date source.** `verified_facts` gains a nullable `fact_date`
   column, populated only when `fact_type = 'date'`. A timeline event's
   date is *read from* an existing verified fact's `fact_date` — never
   re-entered by hand, never derived from raw text or an
   `ai_observations` row directly. This required extending Phase 3.5's
   fact layer (see §3, Step 0) — the date-parser (Phase 3.5 Step 3)
   already computes a real `datetime.date` internally before formatting
   it into `statement` text; it was simply never preserved structurally
   until now.
2. **No system-suggested events in v1.** Every `timeline_events` row is
   created by a human explicitly selecting facts. `created_by` is always
   `"manual"`; `status` is always `"confirmed"` at creation. The columns
   stay in the schema (matching `DATA_MODEL.md`) for a possible future
   suggestion queue, but nothing populates the other values yet.
3. **`timeline_events` gets `deleted_at`.** The documented schema in
   `DATA_MODEL.md` omits this column, which looks like a gap against
   design principle #3 ("nothing is hard-deleted") and against every
   other case-scoped entity built so far (`Document`, `Annotation`,
   `VerifiedFact`). Soft-delete only; no hard-delete path anywhere.
4. **Incremental fact attachment.** An event starts with ≥1 fact
   (exactly one of which is its date source) and can gain more
   supporting facts later via a separate action, mirroring how tags and
   annotations already accrue over time in this app.

## 2. What a timeline event actually is

A `timeline_events` row always has exactly one **date-source fact** — an
existing `verified_facts` row with `fact_type = 'date'` and a non-null
`fact_date` — plus zero or more additional **supporting facts** of any
fact type. The event's `event_date` is copied from the date-source
fact's `fact_date` at creation time and never changes afterward (an
event cannot be re-anchored to a different fact in v1 — only removed
via soft-delete and recreated, if a correction is needed).

This makes the traceability chain from Phase 3.5's design carry all the
way through: `timeline_events` → `timeline_event_facts` → `verified_facts`
→ `verified_fact_citations` → `citations` → `document_pages`/`documents`.
Nothing here reads `ai_observations` or `citations` directly — only
through an already-human-confirmed `verified_facts` row, exactly as the
standing rule requires ("no direct timeline generation from raw
extracted text or observations").

`event_date_precision` (exact/approximate/range) and
`event_date_range_end` are entered independently by the human at event
creation — `verified_facts.fact_date` is a single point-in-time value
with no precision/range concept of its own; if an event's date is
genuinely fuzzy, that fuzziness is expressed on the event, not the fact.
`event_date_source` is always the fixed string `"verified_fact"` in v1
(kept as a column for schema symmetry with `documents`' three-value
enum, but not meaningfully variable while system-suggested events are
out of scope).

## 3. Schema

### Step 0 — extend the fact layer (touches Phase 3.5 files)

- `ai_observations.observed_date` — nullable `DateTime(timezone=True)`.
  Set by the date-parser (`app/core/facts/date_extraction.py`) from the
  `datetime.date` it already computes per match; null for any future
  non-date observation source.
- `verified_facts.fact_date` — nullable `DateTime(timezone=True)`. Set:
  - by `create_verified_fact()` when a human directly asserts a
    `fact_type='date'` fact — **required** in that case (raises
    `ValueError` if a date-type fact is asserted with no date), and
    **required-absent** for every other fact type (raises if a non-date
    fact type is given a date — the invariant "non-null iff
    `fact_type='date'`" is enforced in code, not a DB constraint, same
    style as every other validation in this app).
  - by `promote_observation()`, copied from `observation.observed_date`
    by default, with an optional override parameter (mirroring the
    existing `statement` override).

This step touches `app/db/models.py`, one migration, `app/core/facts/service.py`
(`create_verified_fact`, `create_ai_observation`, `promote_observation`
signatures), `app/core/facts/date_extraction.py` (sets `observed_date`),
the manual "assert fact from citation" route/form (`app/api/facts.py`,
`document_viewer.html` — gains a date input, required only when
`fact_type='date'` is selected), and the existing Phase 3.5 tests that
construct `fact_type='date'` facts without a date (updated, not
regressed — this is a deliberate strengthening).

### Step 1 — timeline schema (new)

- `event_types` — seeded lookup, same pattern as `fact_types`/`document_types`/
  `annotation_types` (`type_id`, `name`, `description`, `is_active`).
  Seed list: meeting, evaluation, incident, communication, decision,
  deadline, other — matching `DATA_MODEL.md`'s example list.
- `timeline_events` — `event_id` (PK), `case_id` (FK), `event_date`
  (`DateTime(timezone=True)`, **NOT NULL** — every event is a date by
  construction), `event_date_range_end` (nullable), `event_date_precision`
  (NOT NULL, default `"exact"`), `event_date_source` (NOT NULL, always
  `"verified_fact"` in v1), `title` (NOT NULL), `description` (nullable),
  `event_type_id` (FK), `created_by` (NOT NULL, always `"manual"`),
  `status` (NOT NULL, always `"confirmed"`), `deleted_at` (nullable —
  §1 decision 3).
- `timeline_event_facts` — `event_id` (FK), `fact_id` (FK →
  `verified_facts`), `is_date_source` (Boolean NOT NULL, default False).
  Composite PK `(event_id, fact_id)`. Exactly one row per event has
  `is_date_source=True`, enforced in the core module at creation time
  (not a DB constraint — same style as "at least one citation" in
  Phase 3.5).

## 4. Core module — `app/core/timeline/service.py` (new)

- `create_timeline_event(db, case, event_type_name, title, description, date_fact_id, additional_fact_ids, event_date_precision, event_date_range_end, actor) -> TimelineEvent`
  Validates: `date_fact_id` refers to a non-deleted `verified_facts` row
  in this case with `fact_type='date'` and non-null `fact_date`; every
  id in `additional_fact_ids` refers to a non-deleted fact in the same
  case (dedup against `date_fact_id`); `title` non-empty. Copies
  `event_date` from the date-source fact. Writes `timeline_event_facts`
  rows (`is_date_source=True` for the anchor, `False` for the rest) and
  an `audit_log` entry (`timeline_event_created`).
- `attach_fact_to_event(db, event, fact_id, actor) -> TimelineEventFact`
  Adds one more supporting fact (`is_date_source=False`, always).
  Validates the fact belongs to the event's case, isn't deleted, and
  isn't already attached. Writes `audit_log`
  (`timeline_event_fact_attached`).
- `remove_timeline_event(db, event, actor) -> None`
  Soft-delete (`deleted_at`). No detach-a-single-fact path in v1 — only
  whole-event removal. Writes `audit_log` (`timeline_event_deleted`).
- `list_timeline_events(db, case_id, event_type_id=None, start_date=None, end_date=None) -> list[TimelineEvent]`
  Non-deleted events for a case, sorted by `event_date`, with optional
  type/date-range filters — the "filter" half of the exit criteria.
- `compute_date_gaps(events) -> list[int | None]`
  Pure function: for a chronologically sorted event list, the day-count
  gap before each event (None for the first). No threshold or "is this
  a significant gap" judgment baked in — the UI shows the plain number
  and lets the reviewer decide what's significant for their case,
  consistent with this app's "surface, don't judge" stance elsewhere
  (`docs/PRIVACY_SECURITY.md` §9).

## 5. UI

- `GET /cases/{case_id}/timeline` — chronological list, event-type
  filter, date-range filter, gap shown between consecutive rows, each
  event showing its date-source fact and any supporting facts (each
  fact linking to its citations, exactly like `facts_review.html`
  already does).
- `POST /cases/{case_id}/timeline` — create (event type, title,
  description, precision, range end, a select of the case's
  `fact_type='date'` verified facts as the date source, a multi-select
  of other verified facts as supporting evidence).
- `POST /cases/{case_id}/timeline/{event_id}/attach-fact` — incremental
  attachment.
- `POST /cases/{case_id}/timeline/{event_id}/delete` — soft-delete.
- Links from `case_detail.html` and `facts_review.html` (a verified
  fact's row gains an "attach to timeline event" affordance).

## 6. Explicitly excluded from Phase 4

Relationship graph (4.5), missing/conflicting records (5), binder (6),
any LLM/cloud service, editing the content of an already-attached
verified fact, re-anchoring an event to a different date-source fact,
detaching a single supporting fact from an event, system-suggested
events, and any code path that reads `document_pages`/`citations`/
`ai_observations` directly instead of through `verified_facts`.

## 7. Step breakdown (each step: implement, test, full suite, FTS5
guard, scope-creep grep, live smoke test, commit, pause for review —
same process as every prior phase)

- **Step 0** — extend the fact layer with structured dates (§3 Step 0).
- **Step 1** — timeline schema: `event_types`, `timeline_events`,
  `timeline_event_facts`, migration, models, seeding.
- **Step 2** — core timeline module (§4).
- **Step 3** — timeline UI (§5) + links from existing pages.

No code has been written for this phase yet. Awaiting approval to begin
Step 0.
