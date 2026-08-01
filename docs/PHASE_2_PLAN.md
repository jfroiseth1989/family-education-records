# Phase 2 Architecture Plan — Extraction, Search & Annotations

Status: **All six §11 decisions approved. Four additional integrity
requirements folded into the design (§12). Step-by-step implementation
plan added (§13). No Phase 2 code written yet** — implementation begins
only after you approve this final version of the plan.

## 1. Purpose and scope

Phase 2 turns ingested files into searchable, citable text, and adds the
annotation layer (highlights/bookmarks/notes) that depends on the document
viewer this phase builds anyway. It does **not** run OCR, generate any
AI output, build the timeline, the relationship graph, or the binder —
those remain Phase 3 onward, unchanged from `docs/PROJECT_PLAN.md`.

**In scope:**
- Text extraction for PDF, DOCX, plain text/RTF, and email (.eml)
- Page-level storage of extracted text with exact offsets (`document_pages`)
- The `citations` primitive (exact document/page/span reference)
- A deterministic "needs OCR" heuristic (flagging only — no OCR execution)
- Full-text search (SQLite FTS5) over extracted text
- Tags (`tags` / `document_tags`) — needed for search filtering
- The annotation layer (highlights, bookmarks, notes) and its own search index
- The document viewer UI these features are built around
- The date-range schema/UI work carried over from the Phase 1 freeze
  decision (see §3) — implemented first, ahead of everything else above

**Explicitly out of scope (unchanged from the approved roadmap):**
- Running OCR (Phase 3) — Phase 2 only *flags* which pages need it
- The fact/observation/summary layer and its human-review gate (Phase 3.5)
- The chronological timeline (Phase 4)
- The relationship graph (Phase 4.5)
- Missing/conflicting record tracking (Phase 5)
- The evidence binder (Phase 6)
- Any AI/LLM-generated content of any kind
- People/organizations as first-class entities (deferred to Phase 4.5,
  where they become relationship-graph node types — Phase 2's search
  filters by case/date/tag/record type/needs-OCR only, not by person)

## 2. Constraints carried forward from the Phase 1 freeze

These aren't new — restating them here so this plan is self-contained and
nothing in it can quietly drift from what was already decided:

1. Vault path stays configurable (`FERPA_VAULT_PATH`); default unchanged.
2. **No delete path, ever, for evidence records** — extraction, tagging,
   and annotation features must not introduce one. Re-extraction (§5.5)
   replaces `document_pages` rows for a document, which is regeneratable
   derived data, not a source record — this does not conflict with the
   no-delete rule, which applies to `documents`/`originals/` and is
   restated here so the distinction is explicit for implementation.
3. No automatic integrity re-verification — extraction reads a stored
   original but does not add any new verification trigger.
4. No automatic version-linking — extraction/search must not infer or
   suggest version relationships.
5. Date-range design is now final (§3); implementing it is Phase 2's first
   work item.
6. No change to the `document_version_groups.current_document_id`
   FK situation.
7. `document_pages`/`citations` proceed exactly as designed in
   `docs/DATA_MODEL.md` (as amended by this plan for the date pattern,
   which doesn't touch either table).

## 3. Prerequisite: implement date-range support

Design is final (`docs/DATA_MODEL.md` "Date representation pattern",
approved in the Phase 1 freeze addendum). This is a small, additive change
to the already-frozen `documents` table, done **before** any extraction
work starts, so nothing built afterward has to assume a stale date model.

**Migration:** add `document_date_range_end` (nullable `DateTime`) to
`documents`; extend the (application-level, not DB-enum) precision values
to accept `range`. No existing column changes type or is dropped — purely
additive, so it's safe against any existing rows.

**Code changes (small, isolated to the existing date module):**
- `app/db/models.py`: add `RANGE = "range"` to `DocumentDatePrecision`;
  add the `document_date_range_end` column to `Document`.
- `app/core/document_dates.py`: `set_document_date()` gains an optional
  `range_end` parameter. Validation: if `precision == RANGE`, `range_end`
  is required and must be `>= document_date`; if precision is `exact` or
  `approximate`, `range_end` must be `None` (reject a mismatched
  combination rather than silently dropping it).
- `app/api/documents.py` + templates: the date field gains a "this is a
  range" mode exposing a second date input for the end date, on both the
  new-document and new-version upload forms — same "entered fresh per
  version, never inherited" rule already in place stays unchanged.
- Tests: extend `tests/test_document_dates.py` and
  `tests/test_api_documents.py` with range-specific cases (valid range,
  end-before-start rejected, range fields ignored/rejected when precision
  isn't `range`, range independent of file metadata like every other date
  case already tested).

This closes out freeze decision #5 in code. Everything after this section
is new Phase 2 work built on top of it.

## 4. Database schema additions

All additive — nothing in the frozen Phase 1 tables is altered beyond §3.
Each of these matches the design already approved in `docs/DATA_MODEL.md`;
this section is the migration-ordering plan, not a re-design.

1. **`documents` gains:** `page_count` (int, nullable), `has_text_layer`
   (bool, nullable until extraction runs), `needs_ocr` (bool, default
   `false` — true if *any* page needs OCR; independent of whether
   extraction itself succeeded), `ocr_status` (string, nullable — values
   reserved for Phase 3: `not_needed` / `queued` / `done` / `failed`;
   Phase 2 only ever sets `not_needed` or leaves it null, since Phase 2
   doesn't run OCR), **`extraction_status`** (string, default `pending` —
   `pending` / `completed` / `failed` / `unsupported_format`; this is the
   "did the extraction process itself run and complete" axis, deliberately
   separate from `needs_ocr`, which is "does some page's *content* need
   OCR regardless of whether extraction completed cleanly"), and
   **`extraction_error`** (text, nullable — a short message when
   `extraction_status = failed`, surfaced in the UI per §12).
2. **`document_types`-style lookup: none new required this phase** — record
   type is already covered.
3. **`document_pages`** (new) — `page_id`, `document_id` (FK),
   `page_number`, `extracted_text` (nullable), `ocr_text` (nullable —
   column exists now so Phase 3 populates it via `UPDATE`, not a new
   migration, but Phase 2 never writes to it), `extraction_method`
   (`native` / `ocr` / `none`), `extraction_confidence` (nullable — Phase 2
   always null; native extraction doesn't carry a confidence score, only
   OCR will), `char_count`, `needs_ocr` (bool, page-level),
   **`source_sha256`** (string, not null — a snapshot of
   `documents.sha256_hash` at the moment this page was extracted; see §12
   for why this is a snapshot column and not just an implicit join).
4. **`citations`** (new) — `citation_id`, `document_id` (FK), `page_id`
   (FK, nullable), `start_offset`, `end_offset`, `paragraph_index`
   (nullable, DOCX), `bounding_box` (nullable JSON — populated only where
   the extractor/annotation UI can supply one; see §7 viewer scope note),
   `quoted_text`. Phase 2 creates rows here only when a user creates a
   span-anchored highlight (§7) — extraction itself doesn't pre-populate
   citations, since a citation represents something someone pointed at,
   not everything extracted.
5. **`tags`** / **`document_tags`** (new) — as designed in
   `docs/DATA_MODEL.md`.
6. **`annotation_types`** (new, lookup) — seeded with `highlight` / `note`
   / `bookmark`, same extensibility pattern as `document_types`.
7. **`annotations`** (new) — as designed, referencing `document_id`,
   nullable `page_id`, nullable `citation_id`.
8. **FTS5 virtual tables** — `document_text_fts` (over
   `document_pages.extracted_text`/`ocr_text`) and `annotation_notes_fts`
   (over `annotations.body_text`), kept separate per the original design
   so a search result is never ambiguous between "found in the source" and
   "found in your notes." **Implementation note:** SQLAlchemy has no native
   ORM construct for FTS5 virtual tables or the triggers that keep them in
   sync with their content tables; these are created via raw `op.execute()`
   SQL inside an Alembic migration. Sync mechanism **approved in §11.1**:
   SQL triggers for `document_text_fts`, explicit application-level
   re-index calls for `annotation_notes_fts`.

**Final migration ordering (revised from the four-migration sketch in the
draft plan, now six — splitting out the FTS5/trigger migrations
specifically because raw-SQL migrations are the highest-review-effort
parts of this phase and shouldn't be bundled with plain ORM-modeled DDL):**

1. Date-range prerequisite (§3) — independent, zero risk to anything else.
2. `documents` column additions + `document_pages` (with `source_sha256`)
   + `citations`.
3. `document_text_fts` virtual table + its sync triggers (isolated on its
   own because triggers are the part of this phase most worth reviewing
   in isolation — see decision 1, approved).
4. `tags` / `document_tags`.
5. `annotation_types` / `annotations`.
6. `annotation_notes_fts` virtual table — no triggers (per approved
   decision 1, annotations are synced by explicit application-level
   reindex, not a trigger), so this is a plain `CREATE VIRTUAL TABLE`
   paired with migration 5's content table.

See §13 for how these map onto reviewable implementation steps.

## 5. Extraction pipeline design

### 5.1 Trigger point
Extraction runs as a **separate step from ingestion**, not folded into
`ingest_document()` — keeping the Phase 1 ingestion contract (copy, hash,
read-only, custody event) unchanged and fast. The API layer calls a new
`extract_document(db, vault, document)` right after a successful ingest,
in the same request. This mirrors the existing separation between
`ingestion/service.py` and `custody.py`: composable, independently
testable functions rather than one large one.

### 5.2 Per-format extractors (`app/core/extraction/`)
- `pdf.py` — PyMuPDF (`fitz`): per-page native text + word bounding boxes.
- `docx.py` — `python-docx`: paragraph-indexed text (one "page" per
  document for DOCX, since it has no fixed pagination — paragraphs are the
  addressable unit via `paragraph_index` on `document_pages`/`citations`).
- `email.py` — stdlib `email`: headers + body as page 1; each attachment
  is ingested as its own child `Document` (reusing `ingest_document()`
  unchanged) and extracted recursively through the same dispatcher.
- `text.py` — plain text and RTF: direct read for `.txt`; RTF via a small
  dependency addition (`striprtf` — new, see §9).
- Images (JPG/PNG/TIFF) — no extractor call; immediately flagged
  `needs_ocr = true` with `extraction_method = none`, since there's no
  text layer to attempt extraction on.
- A `dispatcher.py` selects the extractor by MIME type / file extension
  and defines the "unsupported format" fallback: `extraction_status =
  unsupported_format`, `needs_ocr = false`, `extraction_method = none`,
  `extracted_text` left null — and, per §12, this status is rendered in
  the UI, not just recorded silently in the database.
- **Extractor failures** (a corrupt PDF, a DOCX that fails to parse) are
  caught per-document: `extraction_status = failed`, `extraction_error`
  set to a short, non-sensitive message (the exception type/summary, never
  a raw stack trace shown to the user), zero `document_pages` rows
  written. Per approved decision 6, this never fails the ingestion itself
  — the document is already safely in the vault, hashed and custody-logged,
  before extraction is attempted.

### 5.3 Needs-OCR heuristic
Deterministic, not guessed: a page is flagged `needs_ocr = true` when its
extracted text yield is below a threshold (proposed default: fewer than
~10 extractable words on a page that the source format indicates should
have visible content) — same rule already documented in
`docs/ARCHITECTURE.md` §3.3, now being implemented as written. The
threshold is a named constant, not hardcoded inline, so it can be tuned
without touching extractor logic.

### 5.4 Custody logging and the hash link (§12 requirement)
Extraction appends a `document_custody_events` row (`event_type =
"extracted"` or `"extraction_failed"`) per document, same pattern as every
other action — recording method/page count/error in `details`. Because
every custody event already carries `sha256_hash_at_event` (an existing
Phase 1 field, not new), this event is itself proof of which exact byte
content extraction ran against. `document_pages.source_sha256` (§4)
provides the same linkage at the more granular page level, so a page's
row is independently traceable back to its source document's hash without
requiring a join through `documents` — two layers of the same guarantee,
matching the redundancy already built into the custody-log design.

### 5.5 Re-extraction
Approved: no manual "re-extract" UI action ships in Phase 2, but the
architecture is built to support one without a redesign when it's needed
(e.g. in Phase 3, or if an extraction bug fix warrants reprocessing
existing documents): `extract_document()` is idempotent by design —
it replaces that document's `document_pages` rows (matched and deleted by
`document_id`, keyed off the same `source_sha256` so a re-extraction can
verify it's still operating against the same original content, then
insert fresh rows) rather than appending duplicates, and distinguishes a
first run from a re-run in its custody event (`extracted` vs.
`re_extracted`). Source file and its hash are untouched either way — only
`document_pages` rows and the `documents.extraction_status`/`page_count`/
`has_text_layer`/`needs_ocr` summary columns are ever rewritten. A future
"re-extract" button is therefore just a new UI entry point calling an
already-idempotent function, not new core logic.

## 6. Search design

- SQLite FTS5 virtual table over `document_pages`, external-content-linked
  back to `page_id` so a result resolves directly to a citable page (per
  the approved design).
- Search UI: a case-scoped search page — query box, filters for record
  type (`document_type_id`), tag, date (now range-aware per §3), and
  needs-OCR status. Results show the matching snippet, the source
  document, and the page number, linking straight to that page in the
  viewer (§7).
- No cross-case search in Phase 2 — search is scoped to one case at a
  time, matching the rest of the UI's case-first navigation. (Cross-case
  search isn't precluded by the schema — `document_pages` joins to
  `documents.case_id` either way — this is a UI scoping choice, not a
  schema limitation, and can be revisited later without a migration.)

## 7. Annotations layer & document viewer

- The document viewer is new UI surface: renders a document's pages
  (rendered PDF pages as images for PDF; formatted text for DOCX/plain
  text/email) with the ability to select text and create a highlight, or
  add a page-level bookmark/note.
- **Viewer scope — approved:** lightweight text-offset-anchored highlights
  (shown as inline marked-up text) for Phase 2 v1, not full spatial
  (bounding-box) PDF.js rendering. `citations.bounding_box` stays nullable
  in the schema either way, so full spatial highlighting remains a
  fast-follow UI enhancement later with **no schema or data migration** —
  existing Phase 2 citations remain valid rows; a future page just starts
  populating a column that was already there.
- Highlights create a `citations` row (exact span) plus an `annotations`
  row referencing it. Bookmarks/notes reference the page (and optionally a
  citation) without requiring a highlighted span.
- Annotation text is indexed in `annotation_notes_fts`, separate from
  `document_text_fts`, exactly as designed.
- Excluded from binder generation by default and never usable as a
  citation source for a future verified fact — restating the existing
  design rule so it's visible in this phase's plan, not just buried in
  `docs/DATA_MODEL.md`. See also §12's restatement of citations as the
  sole source-of-truth reference mechanism.

## 8. Technology additions

| Addition | Purpose |
|---|---|
| `PyMuPDF` (`fitz`) | PDF text extraction + page rendering for the viewer |
| `python-docx` | DOCX paragraph-indexed text extraction |
| `striprtf` | RTF text extraction (small, no native deps) |
| *(stdlib `email`)* | .eml parsing — no new dependency |
| SQLite FTS5 | Built into SQLite — no new dependency, but new raw-SQL migration content (§4) |

No AI/LLM, no cloud OCR/vision API, no HTTP client added — consistent with
`docs/PRIVACY_SECURITY.md` throughout. Every new dependency is a local,
offline library.

## 9. Folder structure additions

```
app/core/
├── extraction/
│   ├── __init__.py
│   ├── dispatcher.py       # format → extractor selection
│   ├── pdf.py
│   ├── docx.py
│   ├── email.py
│   ├── text.py
│   ├── needs_ocr.py        # the yield-threshold heuristic, isolated/testable
│   └── service.py          # extract_document() orchestration + custody logging
├── indexing/
│   ├── __init__.py
│   └── search.py           # FTS5 query building, result → citation resolution
└── annotations/
    ├── __init__.py
    └── service.py           # create/list/soft-delete highlight/note/bookmark

app/api/
├── documents.py              # extraction is triggered inline after upload
│                              # (§5.1) — no standalone extraction route,
│                              # since no manual re-extract UI ships (§11.3)
├── search.py
└── annotations.py

app/web/templates/
├── search.html
├── document_viewer.html     # new, replaces/extends document_detail.html's role
└── ...
```

Matches the folder structure already approved in `docs/ARCHITECTURE.md` —
this phase fills in `core/extraction/` and `core/indexing/`, which were
placeholders until now.

## 10. Testing strategy

Same discipline as Phase 1 — every new core module gets direct unit tests
(extractor correctness against small fixture files, needs-OCR threshold
behavior, FTS5 query correctness) plus end-to-end HTTP tests through
`TestClient`, all against a throwaway vault. New fixture files (a small
sample PDF, DOCX, .eml, .txt, and an image) will live under
`tests/fixtures/` — genuinely synthetic/placeholder content only, nothing
resembling a real record. A specific regression test carries forward
Phase 1's core guarantee into this phase: **extraction must never modify a
stored original's bytes or hash** — the same style of test already used
in `tests/test_ingestion.py`, applied to the new extraction step.

## 11. Decisions — approved

1. **FTS5 sync mechanism** (§4): **Approved.** Triggers for
   `document_text_fts` (extraction is the only writer to `document_pages`,
   a small trigger surface), explicit application-level reindex for
   `annotation_notes_fts` (annotations are written from more UI actions).
2. **Viewer scope** (§7): **Approved.** Lightweight text-offset
   highlighting for Phase 2 v1. `citations.bounding_box` stays nullable in
   the schema so full PDF.js spatial highlighting is a later, purely
   additive UI enhancement — no future migration required for it.
3. **Re-extraction UI** (§5.5): **Approved.** No manual "re-extract"
   button ships in Phase 2, but `extract_document()` is built idempotent
   from the start specifically so that a future UI entry point is a thin
   wrapper around already-correct core logic, not a redesign.
4. **Large-file extraction performance**: **Approved.** Synchronous
   extraction in Phase 2; the background job queue is built once, in
   Phase 3, for both OCR and any large-file extraction case.
5. **RTF as a v1 format**: **Approved.** RTF stays in scope, via the
   `striprtf` dependency.
6. **Unsupported/unparseable file handling**: **Approved.** Ingestion
   always succeeds independent of extraction outcome; a failed or
   unsupported extraction is recorded as `extraction_status = failed` /
   `unsupported_format` with a short `extraction_error` message, and is
   visibly surfaced in the UI per §12 — never silently absorbed.

All six carry into §13's implementation steps as fixed requirements, not
open questions.

## 12. Additional integrity requirements (owner-mandated)

Four requirements set before implementation approval, each mapped to a
concrete design commitment — not general goals, but specific things a
reviewer can check for in the code:

### 12.1 Extraction status must be visible in the UI, never silently hidden
- `documents.extraction_status` (§4) is a first-class column, not
  something inferred from the absence of rows elsewhere.
- **Case document list**: a status badge per document — `Extracted (N
  pages)`, `Needs OCR (N of M pages)`, `Extraction failed`, `Unsupported
  format`, or `Pending` — visible in the same table as the existing
  filename/type/version/date columns, not tucked behind a click.
- **Document detail page**: a dedicated "Extraction" panel (parallel to
  the existing "Chain of custody" panel) showing status, page count, which
  pages need OCR, and — when failed — the `extraction_error` message.
- The existing custody log continues to record `extracted` /
  `extraction_failed` / `re_extracted` events, but the *current* status
  must be readable from the document's own page at a glance, not only by
  reading the custody log's history.

### 12.2 Every extracted page must link back to the original document's hash
- `document_pages.source_sha256` (§4) snapshots `documents.sha256_hash` at
  extraction time, independent of the `document_id` foreign key —
  matching the same "snapshot, don't just rely on a live join" pattern
  already used by `document_custody_events.sha256_hash_at_event`.
- Because `documents.sha256_hash` never changes after creation (Phase 1
  guarantee, unaffected by this phase), `document_pages.source_sha256`
  should always equal it — which makes the column doubly useful: it's both
  an explicit, independently-queryable link and a cheap correctness check
  a future integrity job could run (flag any row where they've diverged,
  which should never happen, but would indicate something worth
  investigating immediately if it ever did).

### 12.3 Extracted text must never replace or modify the original file
- Already true by construction in the Phase 1 ingestion/custody code this
  phase builds on top of, and extraction only ever *reads* a stored
  original (§5.1–5.2 — no extractor module opens a stored file in write
  mode). Restated here as a locked requirement, with a dedicated
  regression test (§10) verifying a stored original's hash is identical
  before and after extraction runs, mirroring the equivalent Phase 1 test
  for ingestion.

### 12.4 Citations remain the sole source-of-truth reference mechanism
- Phase 2 introduces no alternate way to "point at" a piece of source
  text. Highlights go through `citations` (§7); search results resolve to
  a `page_id` (§6), which is exactly what a citation references. Nothing
  added in this phase creates a second, competing reference concept.
- This matters beyond Phase 2: `verified_facts`, `timeline_events`, and
  the relationship graph (Phase 3.5 onward, per `docs/DATA_MODEL.md`) are
  all designed to cite through this same table. Phase 2 is the phase that
  actually builds `citations` for the first time, so this requirement is
  really "build it exactly as the already-approved design specifies, with
  no shortcut or parallel structure" — restated here because it's the
  first phase where that design gets tested against real code, not just
  reviewed on paper.

## 13. Implementation plan — reviewable steps

Six steps, each corresponding to one migration from §4 plus the code and
tests built on top of it. Each step is independently testable and
independently reviewable — the suite stays green after every step, not
just at the end. Default proposal: **I'll implement and report after each
step, pausing for your review before starting the next, unless you'd
rather I complete all six in one pass and report at the end** — say which
you prefer when you approve.

### Step 0 — Date-range support (§3)
Migration: add `document_date_range_end` to `documents`. Code:
`DocumentDatePrecision.RANGE`, `set_document_date()` range validation,
upload-form range UI, tests. *No dependency on anything below — this is
the smallest, lowest-risk step and closes out freeze decision #5.*
**Exit check:** existing Phase 1 test suite still fully green; new
range-specific tests pass; a document can be ingested with a range date
end-to-end through the UI.

### Step 1 — Extraction core + `document_pages` + `citations`
Migration: `documents` extraction columns, `document_pages` (with
`source_sha256`), `citations`. Code: `app/core/extraction/` (dispatcher +
per-format extractors + needs-OCR heuristic + `extract_document()`
orchestration with idempotent re-extraction and custody logging), wired
into the upload flow right after ingestion. UI: extraction status visible
per §12.1 (list badge + detail panel), even though nothing can create a
citation yet (that's Step 4's highlight UI) — `citations` rows in this
step come only from tests, not yet from any UI action.
**Exit check:** uploading a PDF/DOCX/TXT/EML/RTF produces correct
`document_pages` rows with accurate `source_sha256`; an unparseable file
ingests successfully with `extraction_status = failed` and a visible
error; a source file's hash is provably unchanged after extraction (§12.3
regression test); the needs-OCR heuristic correctly flags a low-text-yield
page in a test fixture.

### Step 2 — `document_text_fts` + search
Migration: `document_text_fts` virtual table + sync triggers. Code:
`app/core/indexing/search.py`, search API route, search UI page (query +
filters: record type, tag *pending Step 3*, date incl. range, needs-OCR).
**Exit check:** searching returns correct snippets and page-accurate
links for extracted documents; a trigger-driven insert/update/delete on
`document_pages` is reflected in search results without any manual
reindex call; deleting is N/A (no delete path exists per the frozen
no-delete rule) but an extraction re-run (Step 1's idempotent replace)
correctly updates the index via the same triggers.

### Step 3 — Tags
Migration: `tags` / `document_tags`. Code: tag CRUD (add/remove tag on a
document, list case tags), wired into the search filters added in Step 2.
**Exit check:** a document can be tagged, search can filter by tag, tags
are scoped per case.

### Step 4 — Annotations + document viewer
Migration: `annotation_types` / `annotations`. Code:
`app/core/annotations/service.py`, the document viewer UI (§7's approved
lightweight text-offset highlighting), highlight creation (writes a
`citations` row + an `annotations` row, satisfying §12.4), bookmark/note
creation.
**Exit check:** a highlight created in the viewer produces both a
`citations` row with correct offsets and a linked `annotations` row; a
highlight/note/bookmark never appears in any binder-related code path
(none exists yet, but nothing added here should assume one will read
`annotations` directly — see §7); the source file is unaffected by any
annotation action.

### Step 5 — `annotation_notes_fts`
Migration: `annotation_notes_fts` virtual table (no triggers — explicit
reindex per approved decision 1). Code: reindex call after every
annotation create/edit, notes-search UI surfaced separately from document
search (§6/§12.4 — never merged into the same result list as source-text
matches).
**Exit check:** a note's text is searchable; a search of document text
never returns a note as if it were a source-document hit, and vice versa.

Each step's migration is a separate, reviewable Alembic revision (per §4's
final ordering) — nothing in a later step's migration is written until the
earlier one is merged, so schema review happens incrementally rather than
against one large diff at the end.
