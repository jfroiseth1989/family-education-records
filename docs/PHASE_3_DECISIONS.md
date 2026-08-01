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

Three migrations, ordered to match the step breakdown in §9 below (revised
from the original plan's two-migration estimate — see §8):

1. **`ocr_jobs`** (Step 0 — job queue). Plain ORM-modeled table.
2. **`citations` ALTER** (Step 1 — OCR execution core, the step where
   `create_highlight()` first becomes able to cite an OCR page). Adds
   `text_source` (`NOT NULL`, backfilled `'native'`) and
   `source_confidence` (nullable). Also where `document_pages` gains
   `ocr_word_boxes` (nullable, additive, no backfill needed — every
   existing row correctly gets `NULL`).
3. **`ocr_corrections`** (Step 3 — correction layer). Plain ORM-modeled
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
