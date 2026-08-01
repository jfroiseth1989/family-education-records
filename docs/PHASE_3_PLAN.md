# Phase 3 Architecture Plan — OCR

Status: **Draft for review — no Phase 3 code exists yet.** Nothing in this
document has been implemented; per the owner's instruction, no OCR code,
migration, or dependency will be added until this plan itself is reviewed
and approved, the same process used for `docs/PHASE_2_PLAN.md`.

## 1. Purpose and scope

Phase 3 gives scanned/image-only pages — the ones Phase 2's extraction
already deterministically flags `needs_ocr = true` — an actual text
transcription, so they become searchable and citable like every other
page. It does not change what counts as "needing OCR" (that heuristic,
`app/core/extraction/needs_ocr.py`, is frozen Phase 2 code and untouched
here); it builds the pipeline that acts on the flag Phase 2 already sets.

Per `docs/PROJECT_PLAN.md`'s roadmap, Phase 3 is: "Tesseract integration;
OCR job queue + background worker; OCR review UI (confidence display,
side-by-side original vs. OCR text, manual correction layer that never
overwrites raw OCR output); reprocessing on demand." This plan works out
exactly what that means at the schema, code, and UI level, using the same
evidentiary-integrity bar every prior phase was held to.

## 2. Constraints carried forward — from the Phase 2 freeze and earlier

Everything locked in `docs/PHASE_1_FREEZE.md` and `docs/PHASE_2_FREEZE.md`
still applies without exception. The ones most load-bearing for this plan:

- **Local-only, offline OCR.** `docs/PRIVACY_SECURITY.md` §2: "OCR runs
  locally via Tesseract — no cloud vision API... because those would send
  document images containing PII to a third party." Not a preference — a
  locked requirement. No network call is introduced anywhere in this plan.
- **OCR text is never conflated with native-extracted text.**
  `docs/DATA_MODEL.md` design principle #2: separate columns, a required
  `extraction_method`, so nothing downstream can mistake a machine guess
  for the document's actual text layer. `document_pages.ocr_text` and
  `extracted_text` already exist as physically separate nullable columns
  from Phase 2 Step 1 — this plan writes to `ocr_text` only, never touches
  `extracted_text`.
- **Raw OCR output is never overwritten by a correction.**
  `docs/PRIVACY_SECURITY.md` §3: "OCR corrections are stored as a separate
  annotation layer, never as an overwrite of the original OCR output or
  the source file." This is the single most consequential constraint on
  §5 below — it rules out the simplest possible design (correct-in-place)
  outright.
- **Every action on a document appends a chain-of-custody event, never an
  update.** Unchanged pattern from `document_custody_events`, used by
  every phase so far (`imported`, `extracted`, `re_extracted`, `tagged`,
  `annotated`, ...). OCR actions follow the same rule.
- **Citations remain the sole source-of-truth reference mechanism**
  (Phase 2 §12.4, still standing). This plan does not invent a second way
  to "point at" a piece of source text — an OCR/corrected citation goes
  through the exact same `citations` table Step 4 already built.
- **The document viewer stays lightweight text-offset, not full spatial
  rendering** (Phase 2 approved decision 2, still standing — no PDF.js
  spatial highlighting is introduced by this plan; see §3 and §11.3 for
  where that tension actually surfaces).
- **No AI/LLM, no timeline, no relationship graph, no binder changes.**
  Those are Phase 3.5 / 4 / 4.5 / later, not this plan — see §3.

## 3. OCR scope and boundaries

**In scope:**
- Tesseract OCR execution (`pytesseract` + the Tesseract binary) for pages
  already flagged `needs_ocr = true` by Phase 2's extraction heuristic —
  no change to that heuristic or to what triggers a flag.
- A background job queue (`ocr_jobs`) and in-process worker — the first
  background execution model in this app (Phase 2 approved decision 4
  deliberately deferred this exact work to Phase 3, for both OCR and any
  future large-file extraction case).
- Raw OCR text storage in the existing, currently-unwritten
  `document_pages.ocr_text` / `extraction_confidence` columns.
- A dedicated, append-only correction layer (new `ocr_corrections` table,
  §5) — corrections never overwrite raw OCR output.
- An OCR review UI: per-page OCR status/confidence, the OCR text next to
  the original page, and a correction entry form.
- Manual re-OCR ("reprocess") on demand, reusing the idempotent
  replace-on-rerun pattern already proven by Phase 2 Step 1 extraction.
- Search integration for both raw and corrected OCR text (§9), and citing
  OCR/corrected text through the existing `citations` mechanism (§6).
- Custody logging and failure handling for every OCR action (§7).

**Explicitly excluded from Phase 3:**
- **No AI/LLM entity, date, or category extraction** from OCR or corrected
  text. `ai_observations`/`verified_facts` and the human-review promotion
  workflow are Phase 3.5, a separate, later plan.
- **No automatic promotion of anything to a verified fact.** OCR and
  correction both produce *text*, not facts — same as native extraction.
- **No timeline** (Phase 4) and **no relationship graph** (Phase 4.5) —
  no schema, code, or UI for either is touched by this plan.
- **No binder/export changes.** OCR'd and corrected text simply becomes
  citable, exactly like native text already is; binder assembly itself is
  a later phase, untouched here.
- **No cloud OCR fallback of any kind**, ever, for any confidence level —
  locked by `docs/PRIVACY_SECURITY.md` §2 permanently, not a Phase 3
  choice to make.
- **No full spatial/bounding-box highlighting UI.** The viewer stays
  text-offset-only (Phase 2 approved decision 2). §11.3 flags a narrow,
  optional exception worth an explicit decision: whether to *capture*
  Tesseract's word-level bounding boxes now (cheap, additive) even though
  no UI will render them yet.
- **No automatic retry policy for failed OCR jobs.** Retry is a manual,
  logged action — see §7.

## 4. Database schema — what's already there vs. what's new

A significant advantage this phase has, confirmed by re-reading the actual
schema this session: **`documents.ocr_status`, `document_pages.ocr_text`,
and `document_pages.extraction_confidence` already exist**, added
(reserved, nullable, never written) by Phase 2 Step 1 specifically so this
phase wouldn't need to `ALTER` either of those tables. Phase 3 is simply
the first writer of three already-migrated columns — the same pattern
`citations` followed until Step 4 became its first writer.

**No changes to any existing table.** Two new tables only:

### `ocr_jobs` (new)
One row per OCR run of one document (matching `docs/DATA_MODEL.md`'s
original sketch, refined here):
- `job_id` (PK)
- `document_id` (FK → `documents`)
- `status` — `queued` / `running` / `completed` / `completed_with_errors`
  / `failed` (the two-way split between full success and partial success
  is new versus the original two-state sketch — see §7, a multi-page
  document can partially succeed)
- `engine` — e.g. `"tesseract-5.3.0"`, captured at run time so a later
  Tesseract upgrade doesn't retroactively obscure what actually produced
  older OCR text
- `queued_at`, `started_at`, `finished_at` (all nullable until reached)
- `error` — short, non-sensitive message (same convention as
  `documents.extraction_error`), set on `failed`/`completed_with_errors`

Deliberately **no** per-job page-count/progress columns. Progress is
derived by querying `document_pages` directly (how many of this
document's `needs_ocr` pages now have non-null `ocr_text`) rather than
maintaining a duplicate counter that could drift out of sync — the same
reasoning that kept `document_version_groups.current_document_id` as the
only synced-pointer exception in this codebase, not a pattern to repeat
without a specific reason.

### `ocr_corrections` (new)
Append-only correction log — never updated, never deleted, mirroring the
same "ledger, not a mutable field" treatment already given to
`document_custody_events` and `audit_log`:
- `correction_id` (PK)
- `page_id` (FK → `document_pages`)
- `corrected_text` (`NOT NULL`)
- `corrected_by`, `corrected_at`
- *(no `previous_correction_id` chain, no "is_current" flag — see below)*

The **effective corrected text for a page** is simply "the most recent
`ocr_corrections` row for that `page_id`," resolved by a single query
(`ORDER BY corrected_at DESC LIMIT 1`), not a maintained pointer column.
Every prior correction stays in the table permanently — a full edit
history is a free side effect of "append-only," not extra design. This
mirrors the "latest wins, resolved by query" approach already used
throughout this codebase in preference to synced pointers, and keeps
`ocr_corrections` simple enough that it needs no soft-delete column either
(nothing is ever removed from it).

## 5. Raw vs. corrected OCR storage — recommendation

This is the one decision in this plan with real weight, because
`docs/PRIVACY_SECURITY.md` §3 rules out the obvious answer.

**Rejected: correct `document_pages.ocr_text` in place.** Simplest
possible design — a correction just updates the existing column. Directly
violates the locked constraint ("never as an overwrite of the original
OCR output"). Also loses history: a second correction would silently
erase the first.

**Rejected: store corrections as rows in the existing `annotations`
table.** `docs/ARCHITECTURE.md` §3.4 and `PRIVACY_SECURITY.md` §3 both
describe corrections as living in "an annotation layer," which reads at
first glance like "reuse `annotations`." But Phase 2 Step 4/5 locked a
specific, tested meaning for that table: annotations are personal working
notes, permanently excluded from citations and from any future binder
path (§12.4). An OCR correction is a different kind of thing — its whole
purpose is to become the *authoritative* text for that page going
forward, searchable and citable, which is exactly what an annotation is
guaranteed never to be. Reusing `annotations` literally would force a
choice between two already-locked invariants: either corrections become
uncitable (defeating their purpose) or annotations become citable
(breaking Step 4/5). Reading "annotation layer" as *pattern* (additive,
non-destructive, layered on top) rather than *literal table* resolves the
conflict without touching either lock.

**Recommended: the new `ocr_corrections` table from §4, with an explicit
resolution order used everywhere page text is read:**

```
effective_text(page) =
    latest ocr_corrections row for page.page_id, if one exists
    else page.ocr_text  (raw OCR)
    else page.extracted_text  (native)
    else None  (nothing extracted/OCR'd yet)
```

This single resolution function belongs in one place —
`app/core/ocr/text.py` or similar — and every consumer (viewer, search
result rendering, highlight/citation creation) calls it rather than each
re-deriving "which text wins." Centralizing this the same way Step 4
centralized `quoted_text` derivation in one function is a deliberate
requirement, not a suggestion — see §12's risk about this drifting into
three slightly different implementations otherwise.

`document_pages.ocr_text` itself is written exactly once per OCR run
(initial run or an explicit re-OCR — see §7) and never touched by a
correction. Raw OCR output is therefore always recoverable, satisfying
the locked constraint literally, not just in spirit.

## 6. Source traceability

Every traceability guarantee Phase 2 built for native extraction (§12.1–
12.4) needs an OCR-specific equivalent. Re-checked against the actual
schema this session — most of the plumbing already exists:

- **`document_pages.source_sha256`** (Phase 2 Step 1) already snapshots
  `documents.sha256_hash` at the time a page row is written, independent
  of the FK join. OCR writes to the *same* page rows extraction already
  created (it fills in `ocr_text` on the existing row for a page already
  flagged `needs_ocr`; it does not insert new page rows), so this
  guarantee carries over with **zero new code** — the snapshot was
  already taken correctly when the row was first created.
- **OCR status is UI-visible, never silently inferred** — same rule as
  Phase 2 §12.1. `documents.ocr_status` rolls up the document's OCR state
  (`none`/`queued`/`running`/`completed`/`completed_with_errors`/
  `failed`, mirroring `ocr_jobs.status`) and is rendered on the document
  detail page's existing "Extraction" panel (extended, not replaced) and
  the case document list, exactly like `extraction_status` already is.
- **Citing OCR/corrected text uses the exact same mechanism as citing
  native text — no second citation concept.** This requires one small,
  explicit extension to Phase 2's `create_highlight()`
  (`app/core/annotations/service.py`): it currently always slices
  `page.extracted_text[start:end]` — the right choice for Step 4, since
  OCR didn't exist yet. Phase 3 needs it to slice `effective_text(page)`
  (§5) instead, so a highlight on an OCR'd or corrected page still
  derives its `quoted_text` server-side, never from client input, exactly
  as already locked for native pages. **This is a real, if small, change
  to previously-frozen Phase 2 code, called out explicitly here rather
  than left implicit** — everything else in this plan is additive-only.
- **Confidence is a first-class, queryable field, not buried in a log.**
  `document_pages.extraction_confidence` (reserved since Step 1) becomes
  the page's aggregate OCR confidence (mean word confidence from
  Tesseract's output), shown next to the OCR text in the viewer and
  review UI — see §11.3 for the aggregate-vs-per-word tradeoff.
- **Every OCR action is chain-of-custody logged.** New custody event
  types: `ocr_queued`, `ocr_completed`, `ocr_failed`, `ocr_corrected`,
  `ocr_reprocessed` — same verb-past-tense, snake_case convention as every
  existing event type, appended to the same per-document ledger.

## 7. Failure handling

Phase 2's rule — "extraction never fails the caller's request; it's
recorded, not raised" (approved decision 6) — applies here too, but OCR
introduces failure modes synchronous extraction never had:

- **Tesseract not installed / binary missing.** An environment-level
  failure, not a per-document one. Checked once at OCR-job-start (not
  per-page), producing one clear, actionable error
  (`ocr_jobs.error = "Tesseract binary not found — see README"`) rather
  than N identical per-page failures. Flagged as an open decision in §14
  whether this is also checked proactively at app startup.
- **Partial failure within a document.** A multi-page scanned document
  can succeed on some pages and fail on others (a corrupt embedded image
  on one page, a render timeout on another). The job processes page by
  page, writing each page's `ocr_text` as it succeeds — a failure on page
  7 doesn't discard pages 1–6's already-completed work. The job's final
  status is `completed` (all flagged pages done), `completed_with_errors`
  (some done, some failed — `error` names which pages), or `failed`
  (nothing completed, e.g. the environment-level Tesseract-missing case).
- **Worker-crash recovery.** This is a genuinely new operational concern
  — Phase 1/2 never needed one, since everything was synchronous,
  in-request. If the app process dies mid-job, a row can be left
  permanently `status = 'running'` with no worker left to finish it.
  Recommended mitigation: at startup, before serving requests, sweep
  `ocr_jobs` for any row still `running` and mark it `failed` with
  `error = "interrupted — application restarted mid-job"`. The document
  is left exactly as it was (any pages already written keep their
  `ocr_text`); the job can be manually retried. No job silently stays
  "running" forever with no way to observe or recover it.
- **Source integrity check before processing.** Mirrors
  `docs/PRIVACY_SECURITY.md` §3's "SHA-256 re-verified before every export"
  — before an OCR job reads a stored original, it re-hashes it and
  compares against `documents.sha256_hash`; a mismatch fails the job
  immediately with a clear error rather than silently OCR'ing altered
  bytes. Same read-only-open discipline as extraction throughout.
- **Retry is manual, not automatic.** A "Retry OCR" action re-runs the
  same idempotent job (§8's reprocessing model) — no automatic
  retry-with-backoff. For a single-user, single-machine tool, an
  unattended retry loop against a persistently broken environment (e.g.
  Tesseract genuinely missing) adds real complexity for no clear benefit;
  a human noticing and clicking retry once the environment is fixed is
  simpler and sufficient. Flagged in §14 in case this judgment should be
  revisited.

## 8. Migration strategy

Two new tables, **zero `ALTER` statements against any existing table** —
confirmed by §4: every column Phase 3 writes to already exists from Phase
2 Step 1. This is a materially lower-risk migration set than Phase 2's
(which needed six migrations, two of them hand-written raw SQL for FTS5).

Both new tables (`ocr_jobs`, `ocr_corrections`) are plain
SQLAlchemy-modeled tables — safe for `alembic revision --autogenerate` in
the sense that Phase 3 introduces no new FTS5 virtual table of its own.
However, **the standing hazard flagged as remaining decision #5 in
`docs/PHASE_2_FREEZE.md` still applies**: any autogenerate run while
`document_text_fts`/`annotation_notes_fts` exist will very likely again
propose dropping them and their SQLite-managed shadow tables, because
autogenerate still has no model for either. Every migration this phase
produces must be manually inspected before applying, exactly as done five
times running in Phase 2 — this plan does not change that practice, only
restates it as a live requirement for Phase 3's own migrations, and
raises again (see §14) whether it's time to add a lightweight automated
guard instead of relying on manual catching indefinitely.

If Phase 3 is implemented as reviewable steps (§15 proposes this,
mirroring Phase 2's six-step process), the two tables would naturally
split across two migrations — `ocr_jobs` with the job-queue/execution
step, `ocr_corrections` with the correction-UI step — rather than one
combined migration, matching Phase 2's precedent of one migration per
reviewable step rather than batching unrelated schema by feature area.

## 9. Interaction with existing search

`document_text_fts` (Phase 2 Step 2) already indexes an `ocr_text` column
— confirmed by re-reading the Step 2 migration and its triggers this
session. The three sync triggers fire on every `document_pages`
INSERT/UPDATE/DELETE and copy both `extracted_text` and `ocr_text`
verbatim into the index. This means:

- **Raw OCR text becomes searchable automatically**, the moment Phase 3's
  OCR job writes `document_pages.ocr_text`, through triggers that already
  exist today. **Zero search-layer schema or trigger changes needed.**
- **Corrected text needs one small, explicit addition** — because
  corrections live in the separate `ocr_corrections` table (§5), not in
  `document_pages`, the existing triggers never see them. Recommended
  mechanism, extending Step 5's already-proven "explicit reindex, no
  trigger" pattern to a second use case: when a correction is saved,
  issue one explicit `INSERT INTO document_text_fts(document_text_fts,
  rowid, extracted_text, ocr_text) VALUES ('delete', ...)` followed by a
  fresh insert using the *corrected* text as that row's `ocr_text` index
  value — while `document_pages.ocr_text` itself, the real column,
  stays exactly as raw OCR left it (§5's constraint, undisturbed). This
  needs no migration to `document_text_fts` — it's the same external-
  content table and columns Step 2 already built, just written to
  explicitly for this one case, the same way Step 5 already writes to
  `annotation_notes_fts` explicitly rather than via a trigger.
  - **One edge case worth flagging plainly:** if a page is later
    re-OCR'd from scratch (§7's reprocessing), Step 2's *trigger* fires
    again on that `UPDATE` and silently re-syncs the index back to the
    fresh raw text, overriding the correction's index entry (the
    correction's row in `ocr_corrections` is untouched and still there —
    only the *search index's* representation reverts). This is arguably
    correct — a fresh OCR run may no longer even match what was corrected
    — but it should be a visible, intentional UI signal ("this page was
    corrected, then re-OCR'd — review the correction again") rather than
    a silent state a user could be confused by. Flagged as an open
    decision in §14.
- **Result provenance display.** `document_pages.extraction_method`
  (`native`/`ocr`/`none`) is already a stored, queryable field. Recommend
  a small, additive change to the existing search results template: an
  "OCR" badge on any result whose page used OCR, so a user scanning
  results can immediately weigh an OCR-derived hit differently from a
  native-text hit — directly in the spirit of "OCR text is never
  conflated with native text" (design principle #2), extended from schema
  into UI. This reuses the *existing* search route/page/result list —
  Phase 3 does not need a parallel search surface the way Step 5's notes
  search deliberately was one, because OCR text is still document text
  from the same source, just lower-confidence, not a categorically
  different kind of content the way a personal note is.

## 10. Background job architecture

The first background execution model in this app — Phase 1 and 2 were
entirely synchronous, in-request. Per `docs/ARCHITECTURE.md`'s
already-decided tech stack: an in-process `ThreadPoolExecutor`, not
Celery/Redis — appropriate for a single-user, single-machine tool, and
avoids adding operational infrastructure with no corresponding benefit
here.

- **Enqueue:** the upload/extraction flow (`app/api/documents.py`, after
  `extract_document()` runs) enqueues an `ocr_jobs` row (`status =
  'queued'`) for any document with at least one `needs_ocr` page, instead
  of running OCR synchronously in the request — the opposite of how
  extraction itself works, deliberately, since OCR is far slower per page.
- **Worker:** a single background loop (`app/jobs/`, per
  `docs/ARCHITECTURE.md`'s planned folder structure) pulls queued jobs
  FIFO (`queued_at` order) and runs them one at a time — no need for
  worker-pool concurrency tuning in a single-user tool; one job at a time
  keeps the failure/recovery model in §7 simple.
- **Visibility:** a minimal jobs status view (new UI surface — nothing
  like it exists today, since nothing was ever async before) shows queued/
  running/completed/failed jobs per case, so OCR isn't a black box the
  user has to guess about. This is a real net-new page, not an extension
  of an existing one.
- **Startup recovery:** the stuck-`running`-job sweep from §7, run once
  at `create_app()` time before the app starts serving requests.

## 11. Technology additions

| Addition | Purpose | Note |
|---|---|---|
| `pytesseract` | Thin Python wrapper calling the Tesseract binary | Pure-Python, pip-installable |
| Tesseract OCR engine (binary) | The actual OCR engine | **Not pip-installable** — a native OS binary the user (or a packaging step) must install separately. This is a real distribution-risk difference from every dependency added in Phase 2 (PyMuPDF/python-docx/striprtf are all self-contained wheels) — see §12. |

No new dependency is needed to *render* a page image for OCR input —
PyMuPDF (already a Phase 2 dependency) renders a page directly to a PNG,
which is handed to `pytesseract` by file path, avoiding an added Pillow
dependency for that specific step (`pytesseract` may still pull Pillow in
transitively as its own dependency — not something this app adds
directly).

No other technology changes. Still no HTTP client, no AI/LLM SDK, no
cloud OCR API dependency of any kind.

### 11.3 Bounding boxes — a narrow, optional decision

Tesseract produces word-level bounding boxes as a normal part of its
output, at essentially no extra cost. `citations.bounding_box` (nullable
JSON) has existed, unused, since Phase 2 Step 1, specifically reserved for
a future spatial-highlighting UI (Phase 2 approved decision 2). Capturing
OCR bounding boxes now — storing them, not building any UI to render them
— is a genuinely free, additive option this phase could take without
violating the "text-offset-only viewer" lock, since storing data and
building a UI on it are separable decisions. Flagged as an open decision
in §14 rather than assumed either way, since it's the kind of thing better
decided once than half-done and revisited.

## 12. Testing strategy

Same discipline as every prior phase — synthetic fixtures generated on
the fly, not committed binary files; both `TestClient` and a live
`uvicorn` smoke test before any step is reported complete; a mandatory
scope-creep grep before every commit (AI/LLM, timeline, relationship
graph, binder, cloud calls — the same checklist used at every Phase 2
step and the Phase 2 freeze).

OCR-specific additions to that discipline:
- Tests must not require the real Tesseract binary to be installed in the
  test environment for the *default* suite to pass — OCR execution itself
  should be mockable/fakeable at the `pytesseract` call boundary, with a
  separate, clearly-marked integration test (skipped by default, like the
  existing root-only permission test, if Tesseract isn't present) that
  exercises the real binary when available. This keeps the fast default
  suite from becoming environment-dependent, without giving up real
  end-to-end coverage entirely.
- A regression test mirroring Step 1's most important one
  (`test_extraction_never_modifies_the_stored_original`): an equivalent
  `test_ocr_never_modifies_the_stored_original`, since OCR reads the same
  stored original extraction does.
- A test proving a correction never touches `document_pages.ocr_text`
  (the direct code-level enforcement of §5's constraint) — same spirit as
  Step 4's test proving annotation removal never touches a linked
  citation.
- A test proving the worker-crash-recovery sweep correctly reclaims a
  `running` job left over from a simulated crash.
- A test proving a partial-failure job leaves already-OCR'd pages intact
  (`completed_with_errors`, not a full rollback).

## 13. Folder/module structure additions

Matching `docs/ARCHITECTURE.md`'s originally-planned structure, which
already anticipated this phase:

```
app/core/
├── ocr/
│   ├── __init__.py
│   ├── engine.py        # pytesseract wrapper: page image -> raw text + confidence
│   ├── text.py           # effective_text() resolution (§5) -- the one place
│   │                      # "which text wins" is decided
│   └── service.py        # run_ocr_job() orchestration -- the OCR analog of
│                          # app/core/extraction/service.py's extract_document()
├── ocr_corrections/       # (or folded into ocr/ -- naming TBD at implementation time)
│   └── service.py         # create_correction(), list_corrections()
app/jobs/
└── worker.py              # the ThreadPoolExecutor loop + startup recovery sweep (§10)
app/api/
└── ocr.py                 # job status routes, correction routes, review UI route
app/web/templates/
├── ocr_review.html        # side-by-side original vs. OCR text, confidence, correction form
└── jobs_status.html        # the new jobs visibility page (§10)
```

## 14. Open decisions needing owner sign-off before implementation

Numbered so they can be answered individually, same convention as every
prior plan's decisions list:

1. **Tesseract installation story.** Detect-missing-and-show-a-clear-error
   at job-run time (§7, minimum viable), vs. also checking proactively at
   app startup, vs. bundling a Tesseract binary per-OS in the start
   scripts. Recommendation: start-time detection only for v1; bundling is
   a packaging-phase concern, not a Phase 3 blocker.
2. **OCR language configuration.** This plan assumes a single fixed
   Tesseract language (English) with no UI to change it. Confirm this is
   correct for your actual documents, or whether multi-language support
   needs to be in scope now rather than added later.
3. **Bounding-box capture (§11.3).** Capture Tesseract's word-level boxes
   now (stored, unused) for a cheaper future spatial-highlighting phase,
   or skip entirely until that phase is actually planned. Recommendation:
   capture now — additive, no UI commitment either way, avoids a second
   migration later purely to add a column that could have been free now.
4. **Re-OCR silently invalidating a correction's search-index entry
   (§9's edge case).** Accept the silent revert (documented, low
   likelihood), or require an explicit UI warning/confirmation before a
   re-OCR proceeds on a page that has any prior correction. Recommendation:
   require the warning — cheap to build, and prevents a confusing
   "my correction search hit disappeared and I don't know why" moment.
5. **The FTS5 autogenerate hazard (carried over from the Phase 2 freeze,
   restated because Phase 3 is where it would next bite).** Keep catching
   it manually per migration (proven reliable 5/5 times), or add a
   lightweight repo-level guard (a test or script that fails a generated
   migration containing `drop_table` for an FTS5-named table) before
   Phase 3's first migration is generated. Recommendation: add the guard
   now — small, one-time cost, removes a recurring manual-review burden
   for every migration from here forward, not just this phase's two.
6. **Manual-retry-only for failed OCR jobs (§7).** Confirm this judgment,
   or decide a bounded automatic retry (e.g. one retry after N minutes)
   is worth the added complexity.
7. **Aggregate vs. per-word confidence.** `extraction_confidence` as a
   single page-level number (matches the existing reserved column,
   simplest) vs. richer per-word confidence data (better review UX,
   needs a new column/table). Recommendation: aggregate-only for v1,
   consistent with the schema already in place; per-word can be added
   later purely additively if the review UI turns out to need it.

None of the seven block starting Phase 3 technically — they're either
already-reasoned recommendations awaiting a yes/no, or genuinely
open questions only you can answer (2, 6). Recommend resolving all seven
before implementation begins, the same way Phase 2's six flagged
decisions were resolved before any Phase 2 code was written.

## 15. Proposed implementation steps (for when this plan is approved)

Not yet approved — included here so the review of *this plan* can also
weigh in on how it would be broken into reviewable steps, mirroring
Phase 2's six-step process (implement one step, pause for review, repeat).
Proposed split, each independently testable:

- **Step 0 — Job queue + `ocr_jobs`.** Migration, `app/jobs/worker.py`,
  enqueue-on-upload, startup recovery sweep, jobs status page. No OCR
  execution yet — provable with a fake/no-op job to prove the queue
  mechanics work in isolation first.
- **Step 1 — OCR execution core.** `pytesseract` integration,
  `run_ocr_job()`, raw `ocr_text`/`extraction_confidence` writes,
  per-page partial-failure handling, custody events.
  `test_ocr_never_modifies_the_stored_original` lands here.
- **Step 2 — Search integration.** Confirm/extend `document_text_fts`
  behavior for raw `ocr_text` (likely needs no code, per §9 — this step
  is mostly verification), add the OCR-provenance badge to search
  results.
- **Step 3 — Correction layer.** `ocr_corrections` migration,
  `effective_text()`, the explicit reindex extension from §9, the
  extension to `create_highlight()` from §6.
- **Step 4 — OCR review UI.** The side-by-side review page, confidence
  display, correction entry form, re-OCR ("reprocess") action.

---

Holding here. No Phase 3 schema, code, or dependency will be added until
you review this plan and its open decisions in §14.
