# Phase 3 Decisions — OCR

Status: **Decisions finalized. Implementation not started.** This document
resolves the seven open items from `docs/PHASE_3_PLAN.md` §14, incorporates
the owner's directives on raw/corrected storage, traceability, and search
behavior, and is the authoritative record of what Phase 3 will build before
any of it is written. Where a decision here changes a claim made in
`docs/PHASE_3_PLAN.md`, that's called out explicitly (§8) rather than left
as a silent inconsistency between the two documents.

## 1. Decision: Raw vs. corrected OCR storage

**Status: Approved as recommended.**

**Recommendation.** Raw OCR output (`document_pages.ocr_text`) is written
once per OCR run and never modified by a correction. Corrections live in a
new, append-only `ocr_corrections` table (one row per edit, never updated
or deleted). The "current" corrected text for a page is resolved at read
time — the most recent `ocr_corrections` row for that page — rather than
tracked by a maintained pointer column. A single `effective_text(page)`
helper (§2 of the plan's `app/core/ocr/text.py`) is the one place "which
text wins" is decided: correction (latest) → raw OCR → native → none.

**Tradeoffs.** Read-time resolution (a small join/subquery) versus a
maintained "current correction" pointer column: the read cost is trivial
at this app's scale (one user, one case at a time, page-level granularity),
and it avoids a second synced-pointer invariant to keep correct — this
codebase already has exactly one of those
(`document_version_groups.current_document_id`, flagged at the Phase 1
freeze as an accepted, deliberately singular exception) and there's no
reason to add a second. The cost of this choice is that every consumer of
page text must go through `effective_text()` rather than reading
`ocr_text` directly — a discipline requirement, not a technical one.

**Privacy/evidence impact.** This is the decision with the most direct
evidentiary consequence: raw Tesseract output is permanently recoverable,
so a dispute over "what did the OCR actually say before it was edited" can
always be answered from the database, not just from memory or a changelog
entry. Every correction is itself permanently retained (append-only), so a
correction history is a free side effect, not an extra feature — if a
correction is later found to be wrong, that's visible too, not silently
overwritten a second time.

**Implementation impact.** One new table (`ocr_corrections`), no changes
to `document_pages`. `create_highlight()` (Phase 2, `app/core/annotations/
service.py`) is extended to call `effective_text()` instead of reading
`extracted_text` directly — see Decision 2.

## 2. Decision: Traceability and citation distinguishability

**Status: Approved, with a schema addition to meet the distinguishability
requirement.**

**Recommendation.** Source linkage continues through
`document_pages.source_sha256` exactly as designed in Phase 2 — no change
needed there, since OCR writes to the same page rows extraction already
created and hash-stamped. To satisfy "OCR/corrected text citations remain
distinguishable from original document citations," `citations` gains two
new columns, both **snapshotted at citation-creation time** (matching the
same "snapshot, don't rely on a live join" pattern already used by
`document_pages.source_sha256` and every `document_custody_events`
snapshot column):

- `text_source` — `native` / `ocr_raw` / `ocr_corrected`, `NOT NULL`. Set
  once, when the citation is created, based on which branch of
  `effective_text()` actually supplied the sliced text. A citation's
  provenance is fixed at the moment it's made — if the page is
  re-OCR'd or corrected again afterward, the citation still correctly
  says what it was actually quoting *at the time*, the same way
  `quoted_text` itself is already "stored redundantly for display/audit
  even if underlying text is later re-extracted" (`docs/DATA_MODEL.md`,
  `citations` design).
- `source_confidence` — nullable float, set only when `text_source` is
  `ocr_raw` or `ocr_corrected` (always `NULL` for `native`). Snapshots
  `document_pages.extraction_confidence` at citation time, so a citation
  built on a 40%-confidence OCR guess carries that context permanently,
  even if the page's own confidence later changes on re-OCR.

Every existing pre-Phase-3 citation is unambiguously `native` (OCR didn't
exist when it was created) — the migration backfills `text_source =
'native'`, `source_confidence = NULL` for all current rows, same backfill
discipline as Step 2/5's FTS migrations.

**Tradeoffs.** This is a real schema change to a table Phase 2 already
shipped and froze — not something the original plan anticipated (§8 below
corrects that). The alternative — deriving provenance dynamically by
joining to `document_pages.extraction_method`/`ocr_corrections` at read
time — was rejected because it would make a citation's displayed
provenance retroactively change as the underlying page is re-OCR'd or
corrected again, which is exactly the kind of drift this app's citation
model has never allowed anywhere else (`quoted_text` itself is already
immutable/redundant for this same reason).

**Privacy/evidence impact.** Directly serves the evidentiary-integrity
goal: anyone reviewing a citation later — the parent, an advocate, in a
worst case a hearing officer — can see at a glance whether a specific
quoted excerpt came from the document's actual text layer, an unreviewed
machine transcription, or a human-reviewed correction, without having to
cross-reference the page's current state. This is the citation-level
equivalent of the "OCR text is never conflated with native text" principle
that's governed this schema since Phase 1.

**Implementation impact.** One `ALTER TABLE citations ADD COLUMN`
migration (two nullable/defaulted columns — a low-risk, additive change,
not a redesign). `create_highlight()` sets both columns based on which
branch of `effective_text()` supplied the text. The document viewer,
search results, and anywhere else `quoted_text` is displayed gain a small,
shared provenance badge — see Decision 3, which uses the exact same
three-way vocabulary (`native`/`ocr_raw`/`ocr_corrected`) so citations and
search results never describe the same concept two different ways.

## 3. Decision: Search behavior across native, raw OCR, and corrected text

**Status: Resolved.**

**The apparent three-way conflict mostly doesn't exist at the page
level.** Re-confirmed against the actual extraction design this session:
`extracted_text` and `ocr_text` are **mutually exclusive per page** —
native extraction only leaves `extracted_text` null and flags
`needs_ocr = true` when it found insufficient text to begin with, and OCR
only ever runs on pages already flagged that way. A single page is never
simultaneously "found by native extraction" and "found by OCR" — so
`document_text_fts`'s two indexed columns (`extracted_text`, `ocr_text`)
never compete against each other for the same page, and FTS5's existing
relevance ranking (`bm25`, unmodified) already handles ordering results
correctly across pages/documents without any Phase 3 change.

**Raw vs. corrected OCR text is a real, but narrower, version of this
question** — both could, in principle, describe the same page. Resolved
the same way the plan's §9 already proposed: at the *search index* level,
only one value is ever present for a given page's `ocr_text` FTS column at
a time. A correction triggers one explicit reindex call
(`INSERT ... VALUES ('delete', ...)` then a fresh insert using the
corrected text) that **replaces** the raw text's index entry — it does not
add a second, competing entry. There is therefore no ranking/priority
mechanism to design for "raw vs. corrected" either: whichever is more
recently indexed (always the correction, once one exists) is simply what
search matches against. Corrected text supersedes raw text in the index
the moment it's saved — full stop, one value at a time.

**Recommendation for ranking/priority across native and OCR(-derived)
hits in a result list.** Do **not** artificially rank native-text hits
above OCR-derived hits of equal FTS5 relevance. Burying a highly relevant
OCR hit beneath a marginally relevant native hit would undermine search's
actual job — finding the right document — for the sake of a caution
signal that belongs on the result itself, not in its sort position.
Instead, every search result carries the same `text_source` badge
described in Decision 2 (`native` shown with no badge — the implicit
default; `ocr_raw` shown as "OCR"; `ocr_corrected` shown as
"OCR — corrected"), so a user scanning results can weigh a hit's
reliability at a glance without the ranking itself being distorted.

**Tradeoffs.** An alternative — down-weighting or visually deprioritizing
OCR hits — was considered and rejected: it optimizes for a caution signal
at the cost of search actually working as a "find the right document"
tool, which is the higher-priority goal for evidence work under time
pressure. Labeling instead of reordering keeps both goals intact.

**Privacy/evidence impact.** A user relying on search to build a case
timeline or gather evidence needs both completeness (don't hide/bury a
real hit) and honesty about reliability (never let an OCR guess look
identical to verified document text) — this design gives both without
trading one for the other.

**Implementation impact.** No `document_text_fts` schema change (unchanged
from the original plan). The reindex-on-correction call from §9 is
unchanged. New: a small, additive change to the search result rendering
template (derive `text_source` per result the same way `citations` now
stores it, via `document_pages.extraction_method` + whether an
`ocr_corrections` row exists for that page) and to the result-list markup
to show the badge. No change to `search_case_documents()`'s query,
filters, or ranking logic itself.

## 4. Decision: Tesseract installation & environment dependency handling

**Status: Resolved — start-time detection only for v1.**

**Recommendation.** Detect a missing/unusable Tesseract binary once, at
OCR-job start (not per page, not proactively at app startup), and fail
that job immediately with one clear, actionable error
(`ocr_jobs.error = "Tesseract binary not found — see README for install
instructions"`). No bundling of a Tesseract binary in this phase.

**Tradeoffs.** Proactive app-startup detection was considered — it would
surface the problem before a user ever tries to use OCR, rather than at
first use — but was rejected for v1 because it would add a startup-path
check for a feature the user may not touch in a given session, and this
app's startup sequence (vault init → migrate → seed → mount routers) has
so far been kept free of anything beyond what's strictly needed to serve
a request; a job-time check keeps that discipline intact and still
surfaces the problem the first time it actually matters. Bundling a
platform-specific Tesseract binary was rejected as out of scope for an
architecture phase — it's a packaging/distribution concern (parallel to
the PyInstaller/code-signing work already deferred to a later phase in
`docs/ARCHITECTURE.md`'s tech stack table), not an OCR design question.

**Privacy/evidence impact.** None directly — this is an operational/
packaging concern, not a data-handling one. Indirect impact: a clear,
non-cryptic error keeps the user from misinterpreting a missing binary as
"OCR doesn't work / lost my data," which matters for trust in a tool
handling sensitive records.

**Implementation impact.** A check inside `run_ocr_job()` before any page
processing begins — one Tesseract availability probe per job run, not per
page.

## 5. Decision: OCR language configuration

**Status: Resolved — single fixed language (English) for v1, revisit if
needed.**

**Recommendation.** Ship with a single, fixed Tesseract language
(`eng`), no UI to change it. If documents in another language need OCR,
that's a real, concrete reason to revisit — not a hypothetical to design
around now.

**Tradeoffs.** A configurable/multi-language setting is straightforward
to add later (Tesseract supports it natively via a language parameter)
and doesn't require a schema change to retrofit — `ocr_jobs.engine` could
just as easily record `"tesseract-5.3.0:eng"` if/when this changes.
Building language selection UI now, before it's known to be needed, would
be exactly the kind of speculative feature this project has consistently
avoided (no feature has been built ahead of a concrete requirement in any
prior phase).

**Privacy/evidence impact.** None — language selection has no bearing on
where data goes or how it's retained; Tesseract's language packs are all
local, offline data files regardless of which one is active.

**Implementation impact.** None beyond hardcoding `lang="eng"` in the
`pytesseract` call — no new schema, no new UI.

## 6. Decision: Bounding-box capture

**Status: Resolved — capture now, no UI commitment.**

**Recommendation.** Capture Tesseract's word-level bounding boxes when
they're produced (effectively free — Tesseract emits them as part of its
normal output) and store them, without building any UI to render them.
`citations.bounding_box` (nullable JSON) has existed unused since Phase 2
Step 1 for exactly this future purpose.

**Tradeoffs.** The only cost is a slightly larger stored payload per OCR
page; there is no design or UI cost, since storing data and building a
UI on top of it are fully separable decisions, and the viewer stays
text-offset-only (Phase 2 approved decision 2, unchanged) regardless of
what's captured. The alternative — skip capture entirely — would mean a
second migration purely to add this data back if a future spatial-
highlighting phase needs it, for a column that could have been populated
for free now.

**Privacy/evidence impact.** None new — bounding boxes are geometric
coordinates on a page image already stored locally in the vault; nothing
about capturing them changes what data is collected or where it lives.

**Implementation impact.** `document_pages` gains a nullable JSON column
for OCR word-box data (or reuses `citations.bounding_box` at citation
time only, not stored at the page level — see §7's schema section for the
concrete choice), populated by the OCR execution step, read by nothing
yet.

## 7. Decision: Failure/retry handling for OCR jobs

**Status: Resolved — manual retry only, with the two other failure modes
from the plan (partial failure, worker-crash recovery) confirmed
unchanged.** See §5 below for the full, consolidated failure/retry design
— this entry records the retry-policy decision specifically.

**Recommendation.** No automatic retry-with-backoff. A "Retry OCR" action
in the UI re-runs the same idempotent job. Failed jobs stay visibly
`failed` (or `completed_with_errors`) in the jobs status view (§5) until a
human acts.

**Tradeoffs.** An unattended automatic retry loop against a persistently
broken environment (Tesseract genuinely missing, a systematically corrupt
file) adds real complexity — backoff scheduling, a retry-count cap, its
own failure-of-the-retry-mechanism edge cases — for a single-user,
single-machine tool where the person who'd fix the underlying problem
(reinstall Tesseract, replace a bad file) is the same person who'd see
the failure. Manual retry is simpler and sufficient at this scale.

**Privacy/evidence impact.** None directly. Indirect benefit: a failed
job stays visibly failed rather than silently retrying in the background
in a way the user might not notice, which keeps the evidence-status
picture ("has this document actually been OCR'd or not") honest and
current rather than eventually-consistent.

**Implementation impact.** A "Retry" button on the jobs status page,
calling the same `run_ocr_job()` entry point used for the original run —
no new code path, just a new UI trigger for an already-idempotent
function.

## Consolidated schema changes

Revises `docs/PHASE_3_PLAN.md` §4 (see §8 below for what changed and why).

**Already exists, first written by Phase 3 (no migration needed):**
`documents.ocr_status`, `document_pages.ocr_text`,
`document_pages.extraction_confidence` — all reserved since Phase 2 Step 1.

**New tables:**

`ocr_jobs` — unchanged from the plan §4:
- `job_id` (PK), `document_id` (FK → `documents`)
- `status` — `queued` / `running` / `completed` / `completed_with_errors` / `failed`
- `engine`, `queued_at`, `started_at`, `finished_at`, `error`

`ocr_corrections` — unchanged from the plan §4:
- `correction_id` (PK), `page_id` (FK → `document_pages`)
- `corrected_text` (`NOT NULL`), `corrected_by`, `corrected_at`

`ocr_text_history` — **new, added by §9's clarification below** (not in
the original plan or in decisions 1–7 above): archives a page's raw OCR
text immediately before a re-OCR run overwrites it, so a superseded raw
OCR value is never simply lost the way a superseded `document_pages` row
already is on native re-extraction (Phase 2 Step 1's approved, deliberate
behavior — see §9.3 for why OCR gets a stronger guarantee here):
- `history_id` (PK), `page_id` (FK → `document_pages`)
- `ocr_text`, `extraction_confidence` — the values being superseded
- `superseded_at`, `superseded_by_job_id` (FK → `ocr_jobs`)

Written once, automatically, by `run_ocr_job()` itself, immediately before
it overwrites a page's existing (non-null) `ocr_text` on a reprocess run
— never on a page's first OCR run, since there's nothing to archive yet.
Never updated or deleted afterward, same append-only treatment as
`ocr_corrections`.

**Existing table gaining columns (new since the original plan — see §8):**

`citations` gains, per Decision 2:
- `text_source` — `native` / `ocr_raw` / `ocr_corrected`, `NOT NULL`,
  backfilled `'native'` for every pre-Phase-3 row.
- `source_confidence` — nullable float, `NULL` for `native`.

**New nullable column, per Decision 6:**

`document_pages` gains:
- `ocr_word_boxes` — nullable JSON, Tesseract's word-level bounding-box
  data for this page, written once per OCR run alongside `ocr_text`.
  Unread by any code shipped in Phase 3 — reserved for a future spatial-
  highlighting phase, the same "reserved, unwritten-by-UI" pattern
  `citations.bounding_box` itself has followed since Step 1.

## Migration plan

Four migrations (revised from three — see §9.3's `ocr_text_history`
addition), ordered to match the step breakdown in the final implementation
plan (`docs/PHASE_3_IMPLEMENTATION_PLAN.md`):

1. **`ocr_jobs`** (Step 0 — job queue). Plain ORM-modeled table.
2. **`citations` ALTER** (Step 1 — OCR execution core, the step where
   `create_highlight()` first becomes able to cite an OCR page). Adds
   `text_source` (`NOT NULL`, backfilled `'native'` via `server_default`
   — see §9.1 for the exact mechanics) and `source_confidence` (nullable).
   Also where `document_pages` gains `ocr_word_boxes` (nullable, additive,
   no backfill needed — every existing row correctly gets `NULL`).
3. **`ocr_text_history`** (Step 1, same step as above — it's part of the
   same OCR-execution-core work, not a separate reviewable step). Plain
   ORM-modeled table.
4. **`ocr_corrections`** (Step 3 — correction layer). Plain ORM-modeled
   table.

Every one of these must be manually inspected before applying — the
standing FTS5 autogenerate hazard (`docs/PHASE_2_FREEZE.md` §8, restated
as remaining decision #5 there and in the plan's §14) is still live for
as long as `document_text_fts`/`annotation_notes_fts` exist, regardless of
whether a given migration touches FTS5 tables itself. Given this is now
confirmed to recur for a sixth, seventh, and eighth migration in a row,
the lightweight-guard option from the plan's §14 decision 5 is worth
revisiting once more as part of Step 0 (cheap, one-time, removes a
recurring manual-review burden) rather than deferred again — flagged here
as a recommendation, not a re-opened decision requiring further sign-off,
since the plan already reasoned through both options.

## Background job architecture

Confirmed as designed in `docs/PHASE_3_PLAN.md` §10, no changes:

- In-process `ThreadPoolExecutor`, one job at a time (no concurrency
  tuning needed for a single-user tool) — per `docs/ARCHITECTURE.md`'s
  already-decided tech stack.
- Enqueue: the upload/extraction flow adds a `queued` `ocr_jobs` row for
  any document with at least one `needs_ocr` page, instead of running OCR
  synchronously in the request.
- Worker: a single background loop (`app/jobs/worker.py`) pulls queued
  jobs FIFO (`queued_at` order).
- Visibility: a new jobs status page (queued/running/completed/failed per
  case) — genuinely new UI surface, since nothing before Phase 3 was ever
  asynchronous.
- Startup recovery: before serving requests, `create_app()` sweeps
  `ocr_jobs` for any row still `running` and marks it `failed` with
  `error = "interrupted — application restarted mid-job"` (any pages
  already OCR'd before the crash keep their `ocr_text` — nothing is
  rolled back, only the job's own status is corrected).

## Failure/retry handling — consolidated

Combines the plan's §7 with Decision 7 above into one complete picture:

| Failure mode | Detection | Result | Recovery |
|---|---|---|---|
| Tesseract binary missing/unusable | Checked once at job start (Decision 4) | Job `failed`, clear error | Manual retry once environment is fixed |
| Source file hash mismatch before OCR | Re-hash and compare to `documents.sha256_hash` before reading, mirroring the export-time re-verification in `docs/PRIVACY_SECURITY.md` §3 | Job `failed` immediately, before any page is processed | Investigate out-of-band tampering; not an OCR bug to retry past |
| Single-page OCR failure within a multi-page document | Caught per page during job execution | Already-completed pages keep their `ocr_text`; job ends `completed_with_errors`, naming the failed page(s) | Manual retry re-processes the whole document (idempotent); already-good pages are simply re-written to the same values |
| Worker/app crash mid-job | Startup sweep for any `running` row | Job marked `failed`, reason `"interrupted"` | Manual retry; already-OCR'd pages before the crash are untouched |
| All failure modes | — | — | **No automatic retry** (Decision 7) — always a manual, logged action, reusing the same idempotent `run_ocr_job()` entry point as the original run and any "reprocess on demand" action |

Every failure and every retry is chain-of-custody logged
(`ocr_queued`/`ocr_completed`/`ocr_failed`/`ocr_reprocessed`), same
convention as every event type in every prior phase — a document's OCR
history is fully reconstructable from its own custody ledger, not just
from the current `ocr_jobs`/`document_pages` state.

## 8. Corrections to `docs/PHASE_3_PLAN.md`

Two claims in the original plan are superseded by decisions made here,
recorded explicitly rather than left as a silent inconsistency between
the two documents:

1. **§4 said "no changes to any existing table."** No longer accurate —
   Decision 2's citation-distinguishability requirement adds
   `text_source`/`source_confidence` to `citations`. This is a small,
   additive, nullable/backfilled change, not a redesign, but it is a real
   `ALTER TABLE` against a table Phase 2 froze.
2. **§8 estimated two migrations.** Revised to three (see "Migration
   plan" above) — the `citations` ALTER is now its own migration, landing
   in Step 1 rather than Step 0 or Step 3.

`docs/PHASE_3_PLAN.md` §15's proposed step breakdown is otherwise
unchanged: Step 0 (job queue) → Step 1 (OCR execution core, now also
carrying the `citations` migration) → Step 2 (search integration) →
Step 3 (correction layer) → Step 4 (review UI).

## 9. Final clarifications

Three specific confirmations requested before implementation, each
checked precisely rather than asserted.

### 9.1 Existing citation migration behavior

**What values will existing Phase 2 citations receive?**
`text_source = 'native'`, `source_confidence = NULL` — for every single
pre-Phase-3 row, no exceptions. Concretely, the migration adds both
columns with SQLite's own `ALTER TABLE ADD COLUMN ... DEFAULT` mechanism
doing the backfill automatically, not a separate `UPDATE` statement:

```python
op.add_column(
    'citations',
    sa.Column('text_source', sa.String(20), nullable=False, server_default='native'),
)
op.add_column(
    'citations',
    sa.Column('source_confidence', sa.Float(), nullable=True),
)
```

`server_default='native'` is deliberate, not incidental — Phase 2 Step 1
hit exactly this class of bug once already (a `NOT NULL` column added via
`ALTER TABLE` without a `server_default`, which breaks on a database that
already has rows). This migration consciously avoids repeating that
mistake: because `text_source` is `NOT NULL`, every existing row needs a
value the moment the column exists, and `server_default='native'`
supplies it as part of the same `ALTER TABLE` statement, atomically —
there's no window where an existing row is column-added-but-not-yet-
backfilled. `source_confidence` stays nullable, so it needs no default at
all — `NULL` is its own correct value for a citation that was never
based on OCR.

**Confirm no existing citations lose provenance.** Confirmed on two
independent grounds:
1. **Correctness of the backfill value, not just its presence.** Every
   citation that exists today was created by Step 4's `create_highlight()`,
   which — before this phase — only ever sliced `page.extracted_text`
   (native text; OCR didn't exist yet). There is no code path anywhere in
   the shipped Phase 1/2 codebase that could have produced a citation from
   OCR text. `'native'` is not a defensive guess for ambiguous data — it's
   the only value that was ever possible for these rows.
2. **The migration touches nothing else.** It is exactly two
   `ADD COLUMN` statements — `quoted_text`, `start_offset`, `end_offset`,
   `document_id`, `page_id`, `citation_id`, and every existing row's
   identity are untouched. This will be locked in by a migration
   regression test mirroring Step 1's
   `test_migrations_apply_cleanly_against_a_populated_documents_table`:
   seed a citation at the pre-Phase-3 revision, run the migration, and
   assert every original column is byte-for-byte unchanged and the two
   new columns read exactly `('native', None)`.

### 9.2 Citation immutability

**Confirmed: `text_source` and `source_confidence` are set once, at
citation creation, and nothing in this plan ever updates them
afterward.** Verified empirically this session, not just designed that
way on paper — `grep`ing the current codebase for every site that
constructs or touches a `Citation` shows exactly one: `Citation(...)` in
`create_highlight()` (`app/core/annotations/service.py`). There is no
citation-edit route, no code path that loads an existing `Citation` and
reassigns any of its fields, for any column, native or otherwise — this
has been true since Step 4 and this plan does not change it. Phase 3's
extension of `create_highlight()` (§2/Decision 2) sets both new columns
at the same `Citation(...)` construction site, the same way every other
column is set — there is no second write path being introduced.

This also answers a related question worth stating explicitly: **a
correction or a re-OCR run on a page never retroactively changes any
citation already made from that page.** A citation created before a
correction keeps recording `text_source = 'ocr_raw'` and whatever
confidence was current then, even after a correction exists — it
accurately describes what was actually quoted *at the time*, not the
page's current state. A new citation made *after* the correction would
correctly get `text_source = 'ocr_corrected'`. Two citations from the
same page can legitimately have different `text_source` values if one
predates a correction and one postdates it — that's not an inconsistency,
it's the intended behavior.

No database-level trigger enforcing immutability is proposed — consistent
with how the rest of this schema already works (`document_custody_events`
and `audit_log` are also append-only by convention and the absence of any
`UPDATE` code path, not by a DB-level `INSTEAD OF UPDATE` trigger).
Adding one for `citations` specifically, when no other table in this
schema has one, would be inconsistent scope for a guarantee that's
already fully enforced by there being no update code path at all — a
migration/unit test asserting this (attempt to reassign a citation's
`text_source` after creation, confirm nothing in the service layer
exposes a way to do so) is the appropriate, precedent-matching level of
rigor.

### 9.3 OCR correction history

**Corrections: fully append-only, confirmed.** `ocr_corrections` rows are
only ever `INSERT`ed — no application code path updates or deletes one.
Every correction ever made to a page stays queryable forever; "the
current corrected text" is just "the latest row," never a destructive
operation on an earlier one.

**Raw OCR history across a re-OCR run: this needed a real design
addition, not just a restatement — flagged plainly rather than glossed
over.** The design as decided through §7 above (matching Phase 2 Step 1's
approved extraction-re-run behavior) had `document_pages.ocr_text`
**replaced** on a reprocess run, the same idempotent pattern extraction
already uses — which means the specific superseded raw OCR text, if nothing
had ever cited it, would not have been separately recoverable after a
re-OCR. That's a materially weaker guarantee than "previous OCR states
remain recoverable," so this plan adds `ocr_text_history` (§ schema
above) to close that gap: **every raw OCR value a page ever had, not just
every correction, is now permanently recoverable.**

Mechanism: immediately before `run_ocr_job()` overwrites a page's
existing (non-null) `ocr_text` on a reprocess, it archives the value
being superseded into `ocr_text_history`, tagged with which `ocr_jobs`
run replaced it. A page's first-ever OCR run archives nothing (there's
nothing to supersede yet). Like `ocr_corrections`, this table is
`INSERT`-only.

**Why OCR gets a stronger retention guarantee here than native
re-extraction still has.** Worth stating plainly rather than leaving an
unexplained asymmetry between two phases: re-running native extraction
against the *same, unchanged, hash-verified original file* should,
correctness bugs aside, deterministically reproduce the same text every
time — archiving superseded `document_pages.extracted_text` rows has low
marginal evidentiary value. A Tesseract OCR run is not deterministic in
the same sense (engine version, configuration, or even incidental
environment differences can change its output between runs on the exact
same image) — so a superseded raw OCR guess is a genuinely different,
independently meaningful artifact, not just a reproducible recomputation.
This plan treats that difference as a reason for a stronger guarantee,
not an inconsistency to resolve by weakening either side.

**Net effect:** between `ocr_text_history` (every raw OCR value a page
ever had) and `ocr_corrections` (every correction ever made), combined
with `citations.quoted_text`/`text_source`/`source_confidence` snapshots
(§9.2 — permanently fixed at the moment each citation was made) and the
chain-of-custody ledger (`ocr_queued`/`ocr_completed`/`ocr_reprocessed`/
`ocr_corrected`, recording *that* each event happened even independent of
the text itself), a page's complete OCR/correction history is
reconstructable from four independent, mutually reinforcing records —
the same layered-redundancy approach already used for hash verification
throughout this app, applied here to OCR text specifically.

## 10. Second round of final clarifications

Three more confirmations requested before approval, on top of §9's three.

### 10.1 OCR history retention

**Are old OCR versions retained indefinitely?** Yes, by construction —
`ocr_text_history` and `ocr_corrections` are both append-only (§9.3);
nothing in this plan ever issues a `DELETE` against either table. Once a
raw OCR value or a correction exists, it's permanent, the same way every
other ledger in this schema is (`document_custody_events`, `audit_log`).

**Is there any cleanup/archive policy?** No, none exists or is proposed.
This is a deliberate consistency choice, not an oversight: no table in
this schema has a retention/expiry policy anywhere — not
`document_custody_events`, not `audit_log`, not (after Phase 2)
`ocr_corrections`'s sibling in spirit, `annotation_notes_fts`'s
underlying `annotations` table (soft-deleted, never purged). Introducing
one uniquely for OCR history would be new, unrequested complexity, and
would sit awkwardly against `docs/DATA_MODEL.md` design principle #3
("nothing is hard-deleted") applied to every other ledger in this app.

Practically: this app's scale is one family's set of case records, not a
high-volume system — even a heavily-corrected, repeatedly-reprocessed
document accumulates a bounded, small number of extra rows, in a SQLite
file living on local disk. Unbounded-growth risk here is theoretical, not
a real operational concern at this scale. If that judgment ever changes
(e.g., a specific reason emerges to prune history), that's a future,
explicit decision to make with a real reason behind it — not something to
design against speculatively now.

### 10.2 Relationship between `document_pages.ocr_text`, `ocr_text_history`, and `ocr_corrections`

The likely source of any ambiguity is that "current" can mean two
different things — "current *raw machine output*" versus "current *best-
known, effective* text." This plan keeps those explicitly separate:

| | Holds | Is it "current"? |
|---|---|---|
| `document_pages.ocr_text` | The most recent raw OCR run's output for this page — always machine-original, **never** edited by a correction | Yes — current *raw OCR*, specifically. This is not the same as "current effective text" if a correction exists. |
| `ocr_text_history` | Every *prior* raw OCR value this page had, archived immediately before each reprocess overwrote it | No — pure historical record. Never read by anything computing "what should be shown/searched/cited right now." |
| `ocr_corrections` | Every correction ever made to this page, append-only | The *latest row* (by `corrected_at`) is the current correction, if any exists. Older rows are historical. Nothing marks a row "current" with a flag — it's derived by querying for the most recent one. |

**The single value anything actually treats as "this page's real text right
now" is `effective_text(page)`** (§3 of `docs/PHASE_3_IMPLEMENTATION_PLAN.md`,
implemented once in `app/core/ocr/text.py`):

```
effective_text(page) =
    latest ocr_corrections row for this page, if one exists
    else document_pages.ocr_text   (current raw OCR)
    else document_pages.extracted_text   (native)
    else None
```

`ocr_text_history` and every non-latest `ocr_corrections` row are
**never** inputs to this function — they exist solely for audit/history
display (e.g., a "view correction/OCR history" panel in the review UI),
not for deciding what the viewer, search index, or a new citation should
treat as the page's text.

### 10.3 Citation behavior — re-confirmed

**Citations always point to the exact text state that existed when they
were created.** Three fields, all set once, at citation creation, and
never touched again:
- `quoted_text` — the literal excerpt (already the existing,
  Phase 2-locked design: "stored redundantly for display/audit even if
  underlying text is later re-extracted," `docs/DATA_MODEL.md`).
- `text_source` — `native` / `ocr_raw` / `ocr_corrected`, which of the
  three text states in §10.2 actually supplied this excerpt.
- `source_confidence` — the OCR confidence at that moment, if applicable.

Together these three fields make a citation fully self-contained: reading
one never requires looking up the page's *current* state to know what it
originally quoted, why, or how reliable that source was at the time.

**Re-OCR or correction never silently changes an existing citation** —
restated from §9.2 because it's worth confirming twice at this stakes
level: verified empirically (not just by design) that exactly one code
site in this entire codebase ever constructs a `Citation`
(`create_highlight()`), there is no edit route, and nothing in this plan
adds a second write path. A correction or reprocess changes what
`effective_text(page)` will resolve to for the *next* citation made on
that page — it has zero effect on any citation already made.

## Confirmation: no Phase 3 code has started

Re-checked directly this session, not asserted from memory:

- `grep -rliE "pytesseract|tesseract|ocr_job|ocr_correction" app/` —
  zero matches.
- `app/core/ocr/`, `app/jobs/`, and any OCR-correction module — none
  exist on disk.
- `pytesseract` is not present in `pyproject.toml`.
- `git status --short` — clean working tree, nothing staged or
  uncommitted.
- `alembic current` — still at `d93ac0658fac` (Phase 2 Step 5's
  migration), no Phase 3 migration exists.

---

Holding here. No Phase 3 schema, code, or dependency will be added until
you approve proceeding with implementation.
