# Phase 3 Implementation Plan — OCR (Final)

Status: **Final plan, approved in principle. Implementation not started.**
This document consolidates `docs/PHASE_3_PLAN.md` (architecture) and
`docs/PHASE_3_DECISIONS.md` (the seven resolved decisions + the three
final clarifications) into one canonical, ready-to-execute reference, so
implementation doesn't require cross-referencing three documents. Where
this document restates something from either source, that source remains
the detailed rationale — this is the execution summary, not a
replacement for the reasoning behind it.

Per the owner's explicit instruction: **implementation does not begin
until this plan itself is approved**, following the exact same
step-by-step, pause-for-review process used for every step of Phase 2.

## 1. What Phase 3 builds

OCR transcription for pages Phase 2 already deterministically flags
`needs_ocr = true`, a background job queue (the app's first async
execution model), a non-destructive correction layer, full historical
recoverability of both raw OCR and corrections, and search/citation
integration — all offline, all local, no exceptions. Full scope/boundary
list: `docs/PHASE_3_PLAN.md` §3.

## 2. Final schema

**No changes to `documents` beyond first-writing an existing column.**
`documents.ocr_status` (reserved since Phase 2 Step 1) becomes the
document-level OCR rollup, mirroring `extraction_status`.

**`document_pages` — one new nullable column, two existing columns
first-written:**
- `ocr_text` (exists, reserved) — raw OCR output, written once per run,
  replaced (not merged) on a reprocess, with the superseded value
  archived first (see `ocr_text_history` below).
- `extraction_confidence` (exists, reserved) — aggregate (mean word)
  confidence for this page's current raw OCR text.
- `ocr_word_boxes` (**new**, nullable JSON) — Tesseract's word-level
  bounding boxes, captured for a future spatial-highlighting phase,
  unread by any Phase 3 UI.

**`citations` — two new columns, both snapshotted at citation-creation
time, never updated afterward:**
- `text_source` — `native` / `ocr_raw` / `ocr_corrected`, `NOT NULL`,
  `server_default='native'`. Every pre-Phase-3 row backfills to `native`
  correctly and provably (`docs/PHASE_3_DECISIONS.md` §9.1) — no existing
  citation loses provenance.
- `source_confidence` — nullable float, `NULL` for `native`, set from
  `document_pages.extraction_confidence` at the moment an OCR/corrected
  citation is created.

**Four new tables:**

| Table | Purpose | Written by | Mutability |
|---|---|---|---|
| `ocr_jobs` | One row per OCR run of one document; status/timing/error | Job enqueue + worker | `status`/timestamps/`error` updated in place as the *one* job progresses — this is the one new table that isn't append-only, since a job's own lifecycle is inherently a single evolving record, not a ledger of many |
| `ocr_text_history` | Archives a page's raw OCR text immediately before a reprocess overwrites it | `run_ocr_job()`, only on a reprocess (never on first run) | Append-only, never updated/deleted |
| `ocr_corrections` | Human corrections to OCR text | Correction UI | Append-only, never updated/deleted |
| — | *(no separate table needed for citation provenance — it's two columns on the existing `citations` table, per above)* | | |

## 3. Effective text resolution — the one rule every consumer follows

```
effective_text(page) =
    latest ocr_corrections row for page.page_id, if one exists
    else page.ocr_text  (raw OCR, current — not historical)
    else page.extracted_text  (native)
    else None
```

Implemented once, in `app/core/ocr/text.py`, called by the viewer, search
result rendering, and `create_highlight()` — no consumer re-derives "which
text wins." `ocr_text_history` and prior `ocr_corrections` rows are
historical record only; they never participate in `effective_text()`,
which always reflects the current state.

## 4. Migrations — four, in this order

1. **`ocr_jobs`** — plain ORM table.
2. **`citations` ALTER** (`text_source`, `source_confidence`,
   `server_default='native'` per §9.1's exact mechanics) — bundled with
   migration 3 in the same implementation step, since both land the
   moment OCR pages first become citable.
3. **`ocr_text_history`** — plain ORM table, plus the nullable
   `document_pages.ocr_word_boxes` column.
4. **`ocr_corrections`** — plain ORM table, its own later step.

Every migration must be hand-inspected before applying — the standing
FTS5 autogenerate hazard (`document_text_fts`/`annotation_notes_fts`
misdetected as extra tables to drop) is still live regardless of whether
a given migration touches FTS5 tables. Recommendation carried from the
decisions doc: add the lightweight repo-level guard (a check that fails a
generated migration containing `drop_table` for an FTS5-named table) as
part of Step 0, rather than relying on manual catching for an eighth,
ninth, tenth time.

## 5. Background job architecture

In-process `ThreadPoolExecutor`, one job at a time — no concurrency
tuning needed for a single-user, single-machine tool. Enqueue on upload
(any document with a `needs_ocr` page gets a `queued` `ocr_jobs` row
instead of synchronous processing). A single worker loop
(`app/jobs/worker.py`) pulls jobs FIFO by `queued_at`. A new jobs-status
UI page shows queued/running/completed/failed per case — genuinely new
surface, since nothing before Phase 3 was ever asynchronous. Startup
recovery: before serving requests, sweep any `ocr_jobs` row still
`running` and mark it `failed` ("interrupted — application restarted
mid-job") — no job is ever silently stuck forever.

## 6. Failure handling and retry — summary table

| Failure mode | Result | Recovery |
|---|---|---|
| Tesseract binary missing | Job `failed`, clear error, checked once at job start | Manual retry after fixing the environment |
| Source file hash mismatch | Job `failed` before any page is processed | Investigate tampering; not a retry-past situation |
| Single-page failure in a multi-page document | Already-OCR'd pages keep their text; job `completed_with_errors`, names the failed page(s) | Manual retry reprocesses the document (idempotent) |
| Worker/app crash mid-job | Startup sweep marks it `failed` ("interrupted") | Manual retry; pages OCR'd before the crash are untouched |
| Any failure | — | **No automatic retry** — always a manual, logged action via the same idempotent `run_ocr_job()` entry point |

Every OCR action is chain-of-custody logged:
`ocr_queued` / `ocr_completed` / `ocr_failed` / `ocr_reprocessed` /
`ocr_corrected` — a document's full OCR history is reconstructable from
its custody ledger alone, independent of current table state.

## 7. Traceability and immutability guarantees — final statement

- **`document_pages.source_sha256`** (existing, Phase 2 Step 1) covers
  OCR pages with zero new code — OCR writes to the same page rows
  extraction already hash-stamped.
- **Citation provenance is permanent.** `text_source`/`source_confidence`
  are set once, at citation creation, by the only code path that ever
  constructs a `Citation` — verified empirically this session
  (`docs/PHASE_3_DECISIONS.md` §9.2), not just designed that way. No
  citation-edit path exists or is introduced. A citation describes what
  was quoted *at the time*, permanently, regardless of later corrections
  or re-OCR runs on that page.
- **Nothing is lost, ever, across a correction or a reprocess.** Corrected
  text: `ocr_corrections`, append-only. Superseded raw OCR text:
  `ocr_text_history`, append-only, added specifically to close the gap
  between "corrections are recoverable" and "all previous OCR states are
  recoverable" (§9.3 of the decisions doc). Every event is additionally
  logged in the custody ledger independent of the text itself — four
  independent, mutually reinforcing records of a page's OCR history, the
  same layered-redundancy approach already used for hash verification
  throughout this app.

## 8. Search integration — final statement

`document_text_fts` already indexes `ocr_text` (Phase 2 Step 2) — raw OCR
becomes searchable via existing triggers with **zero search-layer code
changes**. Corrected text becomes searchable via one explicit reindex
call at correction-save time (extending Step 5's proven "explicit
reindex, no trigger" pattern), which replaces — not adds to — that page's
indexed value, so there is never a competing raw-vs-corrected match for
the same page. Native and OCR text are mutually exclusive per page by
construction (a page is never both natively-extracted-successfully and
OCR'd), so FTS5's existing relevance ranking needs no Phase 3 change.
Search results gain a provenance badge (`native`/`OCR`/`OCR — corrected`,
sharing the same three-way vocabulary as `citations.text_source`) rather
than any ranking bias by source — full detail and rationale in
`docs/PHASE_3_DECISIONS.md` §3.

## 9. Proposed reviewable steps

Same process as every Phase 2 step: implement one, report files/
migrations/dependencies/tests/scope confirmation, pause for review, wait
for approval before the next.

- **Step 0 — Job queue.** Migration 1 (`ocr_jobs`). `app/jobs/worker.py`,
  enqueue-on-upload, startup recovery sweep, jobs status page. Provable
  with a no-op fake job before any real OCR code exists, to validate
  queue mechanics in isolation. Recommended: land the FTS5-autogenerate
  guard script here too (§4).
- **Step 1 — OCR execution core.** Migrations 2+3 (`citations` ALTER +
  `ocr_text_history`, plus `document_pages.ocr_word_boxes`).
  `pytesseract` dependency added. `run_ocr_job()`, raw text/confidence/
  word-box writes, the archive-before-overwrite mechanism, per-page
  partial-failure handling, the `create_highlight()` extension (§7),
  custody events. `test_ocr_never_modifies_the_stored_original` and the
  citation-backfill migration test (§9.1) land here.
- **Step 2 — Search integration.** Verify raw `ocr_text` search
  end-to-end (expected to need no production code, per §8 — this step is
  mostly verification + tests), add the provenance badge to search
  results.
- **Step 3 — Correction layer.** Migration 4 (`ocr_corrections`),
  `effective_text()`, the explicit reindex extension for corrected text,
  the immutability regression test (§9.2).
- **Step 4 — OCR review UI.** Side-by-side original vs. OCR text,
  confidence display, correction entry form, "Retry OCR"/"Reprocess"
  actions with the re-OCR-after-correction UI warning (Decision from the
  plan's original §14 item 4, confirmed in the decisions doc).

## 10. Confirmation: no Phase 3 code has started

Re-verified this session:

```
$ grep -rliE "pytesseract|tesseract|ocr_job|ocr_correction|ocr_text_history" app/
(no matches)
$ find app/core/ocr app/jobs app/core/ocr_corrections
find: no such file or directory (all three)
$ grep -i pytesseract pyproject.toml
(no match)
$ git status --short
(clean before this document itself was added)
$ alembic heads
d93ac0658fac (head)   # Phase 2 Step 5's migration — no Phase 3 migration exists
```

---

Holding here. No Phase 3 schema, code, or dependency will be added until
you approve this plan and its step breakdown.
