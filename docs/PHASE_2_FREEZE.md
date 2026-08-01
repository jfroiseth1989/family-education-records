# Phase 2 Freeze Summary — Extraction, Search & Annotations

Status: **Phase 2 locked.** Approved by owner (Steps 0–5, commits `9fbd319`
through `c949f31` on `claude/ferpa-evidence-architecture-awlfqt`). This
document is the final record of Phase 2 before Phase 3 (OCR) planning
begins. Every claim below was re-verified directly against the repository
this session — a fresh migration run, a full test run, a dependency diff,
and targeted greps — not carried forward from memory of earlier turns.

## 1. Completed Phase 2 steps

Six steps, each implemented, reviewed, and approved individually, in order:

| Step | Scope | Commit |
|---|---|---|
| **Step 0** | Date-range support: `document_date_range_end`, `DocumentDatePrecision.RANGE`, range-aware upload/version UI. Pulled forward from Phase 1's freeze decision #5. | `9fbd319` |
| **Step 1** | Extraction core: per-format extractors (PDF/DOCX/plain text/RTF/email), `document_pages` + `citations` tables, deterministic needs-OCR heuristic, `extraction_status`/`extraction_error` on `documents`, idempotent `extract_document()`, custody events. | `0e76cd4` (freeze note: `5fa645b`) |
| **Step 2** | Search: `document_text_fts` (FTS5, external-content, trigger-synced), `search_case_documents()`, case-scoped search UI with type/tag/date/needs-OCR filters. | `8c355cd` |
| **Step 3** | Tags: `tags`/`document_tags`, case-scoped case-insensitive find-or-create, tag CRUD on document detail, tag filter wired into search. | `8529ccd` |
| **Step 4** | Annotations: `annotation_types`/`annotations`, document viewer (lightweight text-offset highlighting), highlight/note/bookmark creation, soft-delete removal. | `5ee704b` |
| **Step 5** | Notes search: `annotation_notes_fts` (FTS5, external-content, explicit-reindex-synced — no triggers), separate notes-search UI, cross-linked but never merged with document search. | `c949f31` |

Every step was implemented and reported individually, with an explicit
scope confirmation and a pause for review before the next step began — no
step was started without the prior one's approval.

## 2. Final schema additions — introspected directly, not recalled

Ran a fresh `run_migrations()` against an empty database and inspected the
resulting schema this session. Phase 1 ended with 7 tables (§3 of
`docs/PHASE_1_FREEZE.md`); Phase 2 adds 8 more application tables plus two
sets of FTS5 shadow tables (SQLite-internal, not modeled in the ORM):

**`documents` gained** (Step 0 + Step 1, both additive — no Phase 1 column
altered or dropped): `document_date_range_end`, `page_count`,
`has_text_layer`, `needs_ocr` (`NOT NULL DEFAULT 0`), `ocr_status`
(nullable — reserved for Phase 3, never set to anything but null by any
code shipped so far), `extraction_status` (`NOT NULL DEFAULT 'pending'`),
`extraction_error`.

**`document_pages`** (Step 1) — `page_id` PK, `document_id` FK, `page_number`,
`extracted_text` (nullable), `ocr_text` (nullable — reserved for Phase 3,
never written by any code shipped so far), `extraction_method`
(`native`/`ocr`/`none`), `extraction_confidence` (nullable, always null so
far), `char_count`, `needs_ocr`, `source_sha256` (`NOT NULL` — snapshots
`documents.sha256_hash` at extraction time). Unique on
`(document_id, page_number)`.

**`citations`** (Step 1, first written by Step 4's highlight creation) —
`citation_id` PK, `document_id` FK, `page_id` FK (nullable),
`start_offset`/`end_offset` (nullable), `paragraph_index` (nullable, DOCX),
`bounding_box` (nullable JSON — still unpopulated; stays ready for a future
spatial-highlighting UI with no migration needed), `quoted_text`
(`NOT NULL`).

**`document_text_fts`** (Step 2) — FTS5 virtual table over `extracted_text`
and `ocr_text`, external-content-linked to `document_pages`
(`content_rowid='page_id'`), kept in sync by three SQL triggers
(`AFTER INSERT/UPDATE/DELETE ON document_pages`). Because it already
indexes the `ocr_text` column, Phase 3 populating that column will make OCR
text searchable through the existing triggers with **no search-layer
change** — confirmed by re-reading the trigger bodies this session.

**`tags`** (Step 3) — `tag_id` PK, `case_id` FK, `name`, `category`
(nullable), `created_at`. Unique on `(case_id, name)`.

**`document_tags`** (Step 3) — composite PK (`document_id`, `tag_id`),
both FKs, `created_at`.

**`annotation_types`** (Step 4, lookup) — `type_id` PK, `name` (unique),
`description`, `is_active`. Seeded with `highlight`/`note`/`bookmark`.

**`annotations`** (Step 4) — `annotation_id` PK, `case_id` FK,
`document_id` FK, `page_id` FK (nullable), `citation_id` FK (nullable),
`annotation_type_id` FK, `body_text` (nullable), `color` (nullable),
`created_by`, `created_at`, `updated_at` (nullable, unused — no edit
feature exists), `deleted_at` (nullable — soft-delete only).

**`annotation_notes_fts`** (Step 5) — FTS5 virtual table over
`annotations.body_text`, external-content-linked (`content_rowid=
'annotation_id'`), **no triggers** — synced by explicit
`index_annotation_note`/`deindex_annotation_note` calls at each write site
in `app/core/annotations/service.py` (approved decision 1, since
annotations are written from more UI actions than `document_pages` ever
is).

Nothing from Phase 1 was altered or dropped anywhere in Phase 2 — every
change across all six steps is a new column or a new table.

## 3. Migrations added — verified via Alembic's own revision graph

A single linear chain, no branches, confirmed via `alembic history` this
session:

```
15aec3ba1e28  initial schema                              (Phase 1)
      ↓
79c226771ac3  add document date range end                 (Step 0)
      ↓
15160ee5686e  add extraction core: document_pages/citations (Step 1)
      ↓
08c778ee32af  add document_text_fts search index           (Step 2)
      ↓
6375a2b94580  add tags and document_tags                   (Step 3)
      ↓
cd54e558e0d8  add annotation_types and annotations          (Step 4)
      ↓
d93ac0658fac  add annotation_notes_fts search index         (Step 5)  [head]
```

Six new migrations this phase. `run_migrations()` applies whichever of
these a given vault hasn't seen yet, in order — same mechanism used since
Phase 1, no new upgrade machinery introduced.

Two of the six (`08c778ee32af`, `d93ac0658fac`) are hand-written raw SQL,
not autogenerated — SQLAlchemy has no ORM construct for FTS5 virtual
tables or triggers. Every autogenerate run for the other four migrations
(Steps 1, 3, 4, and any re-run after Step 2 shipped) initially proposed
dropping `document_text_fts` and its SQLite-managed shadow tables, because
autogenerate has no model for them; this was caught and the generated
migration hand-trimmed before applying, every time, and verified afterward
by running the full chain and confirming the FTS5 index and its triggers
still function. This is a real, recurring hazard for any future migration
work in this codebase while FTS5 tables exist — worth carrying forward as
a standing caution into Phase 3 and beyond, not just a one-off note.

## 4. Dependencies added — confirmed via `git diff` against the Phase 1 freeze

```diff
 dependencies = [
     ...
+    "pymupdf>=1.24,<2.0",
+    "python-docx>=1.1,<2.0",
+    "striprtf>=0.0.26,<1.0",
 ]
```

All three added in Step 1 (PDF/DOCX/RTF extraction) and unchanged since —
Steps 2 through 5 added zero new dependencies. No PDF/OCR/NLP/HTTP-client
library beyond these three was ever introduced. `pytesseract`/`tesseract`
(Phase 3's actual OCR engine) is not present anywhere in the dependency
list or the codebase.

## 5. Total test status — fresh run this session

```
$ .venv/bin/pytest -q
........................................................................ [ 29%]
........................................................................ [ 59%]
..............................s......................................... [ 88%]
...........................                                              [100%]
242 passed, 1 skipped, 1 warning in ~73s
```

243 tests collected across 23 test files (up from 65 collected / 10 files
at the Phase 1 freeze). The single skip is the same pre-existing
root-only permission-bypass case flagged at the Phase 1 freeze
(`test_make_read_only_prevents_writes_for_non_root_user`) — unrelated to
Phase 2, still not a real gap on a normal user account.

Phase 2 added 13 test files: `test_document_dates.py` extensions (Step 0),
`test_extraction_extractors.py` / `test_extraction_service.py` /
`test_api_extraction.py` (Step 1), `test_indexing_search.py` /
`test_api_search.py` (Step 2), `test_tagging.py` / `test_api_tags.py`
(Step 3), `test_annotations.py` / `test_api_annotations.py` (Step 4),
`test_notes_search.py` / `test_api_notes_search.py` (Step 5).

## 6. Confirmed exclusions — re-checked this session, not carried forward

Grepped the entire `app/` and `tests/` tree fresh this session:

- **AI/LLM**: zero matches for `openai`, `anthropic`, `gpt-`, `llm`,
  `chatgpt`, `claude api`, `ai model`, `summariz` anywhere in application
  or test code.
- **OCR execution**: zero matches for `pytesseract`, `tesseract`,
  `ocr.run`, `perform_ocr`, `run_ocr`. The `needs_ocr` heuristic (a
  deterministic word-count threshold) and the reserved-but-unwritten
  `ocr_status`/`ocr_text` columns are the only OCR-adjacent code that
  exists — flagging, not performing, OCR.
- **Timeline**: zero matches in application code. The only hits anywhere
  in `app/`/`tests/` are (a) three docstring sentences in `models.py`
  naming Phase 4 in passing as a *future* consumer of the citations
  pattern being built now, (b) one similar sentence in
  `document_dates.py`, and (c) sample annotation body text in a test
  ("Ask the advocate about the timeline") — not a feature reference.
- **Relationship graph**: zero matches in application code. The only hits
  are the same two `models.py` docstring sentences noted above, naming
  Phase 4.5 as a future consumer.
- **Binder**: zero matches in application code. The only hit is a
  docstring sentence on the `Annotation` model restating that annotations
  are excluded from any future binder path (§12.4's rule, stated for
  documentation, not implemented).
- **Cloud/network calls**: zero matches for `requests.get/post`,
  `httpx.get/post/Client`, `urllib.request`, `boto3`, `s3.` anywhere in
  `app/`.
- **Local-only binding**: `app/config.py` still binds `127.0.0.1` only —
  unchanged since Phase 1.

## 7. Architecture confirmed still intact

- **Citations remain the sole source-of-truth reference mechanism**
  (§12.4, the requirement most directly tested by Phase 2's own work):
  every `Citation(` construction site outside the model definition and
  test fixtures is in `create_highlight()` — the only writer. Highlight
  `quoted_text` is always derived server-side from the page's own stored
  `extracted_text`, never trusted from client input.
  `annotation_notes_fts`/notes search never mixes into the same result
  list as `document_text_fts`/document search — verified live in Step 5's
  review, not just by code inspection.
- **Extraction never modifies the stored original** (§12.3) — no
  extraction or annotation code anywhere opens a stored original in write
  mode; regression-tested since Step 1, re-verified live at each
  subsequent step's manual smoke test.
- **Every extracted page links back to its source document's hash**
  (§12.2) — `document_pages.source_sha256` has exactly one assignment
  site, unchanged since Step 1.
- **Extraction status is UI-visible, never silently hidden** (§12.1) —
  case list badges and the document detail "Extraction" panel, unchanged
  since Step 1.
- **Annotations are soft-deleted only, and removal never touches a
  linked citation** — verified again in Step 4 and exercised by Step 5's
  deindex path (a removed note drops out of search while its row and any
  citation permanently remain).

## 8. Remaining design decisions before Phase 3 planning

None of these block Phase 3 technically — the schema was built with Phase
3 in mind throughout (see §2's `ocr_status`/`ocr_text`/`document_text_fts`
notes) — but each is worth a conscious sign-off before Phase 3's plan is
written, the same way Phase 1's freeze surfaced open items before Phase 2:

1. **OCR review UI viewer model.** Phase 3's plan (`docs/PROJECT_PLAN.md`)
   calls for a "side-by-side original vs. OCR text" review UI. Step 4's
   document viewer is deliberately lightweight text-offset only (approved
   decision 2) — confirm whether Phase 3's OCR review screen reuses that
   same viewer pattern (rendering `ocr_text` the same way `extracted_text`
   is rendered now) or needs its own, and whether that surfaces the
   deferred spatial/bounding-box rendering question sooner than expected.
2. **OCR job queue design.** `docs/PROJECT_PLAN.md` calls for "OCR job
   queue + background worker." No `ocr_jobs` table or any async/background
   execution model exists yet anywhere in the codebase — Phase 2's
   extraction is entirely synchronous, in-request (approved decision 4,
   deliberately deferred to Phase 3). This is genuinely new architecture
   for Phase 3 to design, not an extension of an existing pattern.
3. **Manual correction layer.** Phase 3's plan requires OCR review/correction
   that "never overwrites raw OCR output." `document_pages.ocr_text` is a
   single column today — confirm whether raw OCR output and a
   human-corrected version need to be two separate columns/rows before
   Phase 3's migration is written, or whether correction is modeled some
   other way (e.g., an annotation-like overlay). This weighs on Phase 3's
   very first migration, so it's the one item here with an actual
   sequencing dependency.
4. **Re-OCR / reprocessing-on-demand UI entry point.** Extraction's
   idempotent replace-on-rerun design (Phase 1/Step 1, approved decision
   3) was built specifically so a future "reprocess" button would be a
   thin UI wrapper, not new core logic. Confirm this same idempotent
   pattern is the intended model for OCR reprocessing too, or if OCR's job
   queue changes that assumption (e.g., queuing instead of synchronous
   replace).
5. **FTS5 hand-migration hazard, now a standing process note.** §3 above
   documents that every autogenerate run while FTS5 tables exist
   mis-detects them as extra tables to drop. This has been caught
   correctly five times in a row across Phase 2, but it's worth explicitly
   deciding whether to (a) keep catching it manually each time (current
   practice, proven reliable so far), or (b) add a lightweight guard (e.g.
   a repo script or test that fails if a generated migration contains
   `drop_table` for an FTS5-named table) before Phase 3 adds its own new
   migrations to the same chain.

---

Phase 2 is frozen as described above. Holding here — no Phase 3 (OCR)
planning or implementation will begin until you review this document.
