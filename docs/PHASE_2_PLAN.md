# Phase 2 Architecture Plan — Extraction, Search & Annotations

Status: **Draft for review — no Phase 2 code written yet.** Phase 1 is
frozen (`docs/PHASE_1_FREEZE.md`). This document is the architecture and
implementation plan for Phase 2 only; implementation waits for your
approval, per `docs/PROJECT_PLAN.md`'s roadmap and your explicit
instruction not to begin Phase 2 coding yet.

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
   `false`), `ocr_status` (string, nullable — values reserved for Phase 3:
   `not_needed` / `queued` / `done` / `failed`; Phase 2 only ever sets
   `not_needed` or leaves it null, since Phase 2 doesn't run OCR).
2. **`document_types`-style lookup: none new required this phase** — record
   type is already covered.
3. **`document_pages`** (new) — `page_id`, `document_id` (FK),
   `page_number`, `extracted_text` (nullable), `ocr_text` (nullable —
   column exists now so Phase 3 populates it via `UPDATE`, not a new
   migration, but Phase 2 never writes to it), `extraction_method`
   (`native` / `ocr` / `none`), `extraction_confidence` (nullable — Phase 2
   always null; native extraction doesn't carry a confidence score, only
   OCR will), `char_count`, `needs_ocr` (bool, page-level).
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
   SQL inside an Alembic migration, and kept in sync either by SQL triggers
   (insert/update/delete on the content table) or by explicit
   application-level re-index calls after extraction/annotation writes.
   **This is an open implementation decision — see §11.**

Migration ordering: §3's date-range migration first (independent, zero
risk to anything else), then one migration for the `documents` column
additions + `document_pages`/`citations`, then `tags`/`document_tags`,
then `annotation_types`/`annotations` + the two FTS5 tables — four small,
independently reviewable migrations rather than one large one.

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
  and defines the "unsupported format" fallback (mark `needs_ocr = false`,
  `extraction_method = none`, leave `extracted_text` null, and surface this
  clearly in the UI rather than failing the whole ingestion).

### 5.3 Needs-OCR heuristic
Deterministic, not guessed: a page is flagged `needs_ocr = true` when its
extracted text yield is below a threshold (proposed default: fewer than
~10 extractable words on a page that the source format indicates should
have visible content) — same rule already documented in
`docs/ARCHITECTURE.md` §3.3, now being implemented as written. The
threshold is a named constant, not hardcoded inline, so it can be tuned
without touching extractor logic.

### 5.4 Custody logging
Extraction appends a `document_custody_events` row (`event_type =
"extracted"`) per document, same pattern as every other action —
recording method/page count in `details`, not a new custody concept.

### 5.5 Re-extraction
If extraction logic changes later (a bug fix, a better PDF library), a
document should be re-extractable without re-ingesting it. Proposed:
`extract_document()` is idempotent — replaces that document's
`document_pages` rows rather than appending duplicates, and logs a
distinct `re_extracted` custody event when it's not the first run. Source
file and its hash are untouched either way. **Open decision: whether a
manual "re-extract" UI action ships in Phase 2 or is deferred until
something actually needs it — see §11.**

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
- **Viewer scope decision for Phase 2 v1** (see §11): full spatial
  (bounding-box) highlighting on rendered PDF pages via a self-hosted
  PDF.js is the richer, more useful experience but the larger UI lift.
  A lighter v1 — text-offset-anchored highlights shown as inline marked-up
  text rather than an image overlay — covers the same underlying data
  model (`citations.bounding_box` stays nullable either way) and is
  faster to ship correctly. Proposing the lighter version for Phase 2,
  with PDF.js spatial highlighting as a fast-follow once the data model is
  proven — but this is genuinely your call given it's mostly a UX
  investment decision, not an architectural one.
- Highlights create a `citations` row (exact span) plus an `annotations`
  row referencing it. Bookmarks/notes reference the page (and optionally a
  citation) without requiring a highlighted span.
- Annotation text is indexed in `annotation_notes_fts`, separate from
  `document_text_fts`, exactly as designed.
- Excluded from binder generation by default and never usable as a
  citation source for a future verified fact — restating the existing
  design rule so it's visible in this phase's plan, not just buried in
  `docs/DATA_MODEL.md`.

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
├── extraction.py?           # or folded into documents.py — see §11
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

## 11. Risks and decisions needing your sign-off before implementation

1. **FTS5 sync mechanism** (§4.8): SQL triggers (always in sync, more
   moving parts in the migration) vs. explicit re-index calls from
   application code (simpler migration, relies on every write path
   remembering to re-index). Recommend triggers for `document_pages`
   (extraction is the only writer, so it's a small trigger surface) and
   explicit re-index for `annotations` (writes happen from more UI
   actions). Confirm or redirect.
2. **Viewer scope** (§7): lighter text-offset highlighting for v1 vs.
   full PDF.js spatial highlighting from the start. Recommend the lighter
   version to ship Phase 2 sooner; revisit after it's in use. Confirm or
   redirect.
3. **Re-extraction UI** (§5.5): ship a manual "re-extract" action in
   Phase 2, or defer until a concrete need appears (e.g. a Phase 3 OCR
   re-run naturally needs something similar, and could introduce it then).
   Recommend deferring. Confirm or redirect.
4. **Large-file extraction performance**: Phase 2 has no OCR and no
   background job queue yet — a very large PDF (hundreds of pages) would
   extract synchronously in the request. Acceptable for expected Phase 2
   volumes, or should a background job queue be pulled forward from
   Phase 3 (where OCR will need one regardless)? Recommend accepting
   synchronous extraction for now and building the job queue once in
   Phase 3 for both OCR and any large-file extraction case, rather than
   building it twice. Confirm or redirect.
5. **RTF as a v1 format**: confirmed in the original Phase 1 planning
   discussion as in-scope; restating here since it's the one Phase 2
   format needing a new (small) dependency. Confirm it's still wanted, or
   drop it and add `.doc`/RTF support later alongside the already-deferred
   legacy formats (`.doc`, `.msg`, spreadsheets).
6. **Unsupported/unparseable file handling**: when an extractor fails
   (corrupt file, unsupported sub-format) — fail the whole ingestion, or
   ingest successfully but leave the document unextracted with a clear
   "extraction failed" status the user can see? Recommend the latter
   (ingestion's read-only/hash/custody guarantees shouldn't depend on
   extraction succeeding) — confirm or redirect.

None of these block writing the plan — they're implementation-approach
choices worth your call before code is written, consistent with how
Phase 1's equivalent decisions were handled.
