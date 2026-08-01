# Phase 2 Step 1 Freeze Summary — Extraction Core

Status: **Step 1 locked.** Approved by owner (commit `0e76cd4` on
`claude/ferpa-evidence-architecture-awlfqt`). This document is the final
record of Step 1 before Step 2 (search/indexing) planning begins. Every
claim below was re-verified directly against the repository this session
— a fresh migration dump, a full test run, and targeted greps — not
carried forward from memory of the implementation turn.

## 1. Final extraction architecture

```
app/core/extraction/
├── types.py        ExtractedPage / ExtractedAttachment / ExtractionResult
│                    -- the shared contract every extractor returns
├── needs_ocr.py     page_needs_ocr(text, has_visual_content) -> bool
│                    deterministic threshold heuristic (<10 words + visual
│                    content present), not a per-format guess
├── pdf.py           PyMuPDF: per-page native text + image detection
├── docx.py          python-docx: single page, paragraph-joined text
├── text.py          plain text (direct read) + RTF (striprtf)
├── email.py         stdlib email: header+body page, attachments returned
│                    as raw bytes (not ingested by this module itself)
├── dispatcher.py    extension -> extractor lookup; recognizes image
│                    extensions separately (no extractor call for those)
└── service.py       extract_document() -- the single orchestration entry
                      point
```

**Trigger point:** `extract_document(db, vault, document, actor)` is
called by `app/api/documents.py` immediately after `ingest_document()`
succeeds, in the same request/transaction — for both new-document uploads
and new-version uploads. It is never invoked from inside
`ingest_document()` itself, keeping Phase 1's ingestion contract
(copy/hash/read-only/custody) unchanged.

**Control flow inside `extract_document()`:**
1. Image extension → flag `needs_ocr=true`, one placeholder `document_pages`
   row (`extraction_method="none"`), `extraction_status="completed"`. No
   extractor is called — there's no text layer to attempt.
2. No dispatcher match → `extraction_status="unsupported_format"`, zero
   pages, no error recorded (not a failure — a recognized non-goal).
3. Dispatcher match, extractor raises → caught broadly, never propagates;
   `extraction_status="failed"`, `extraction_error` set to a short message,
   zero pages.
4. Dispatcher match, extractor succeeds → pages written via
   `_replace_pages()` (delete-then-insert, which is what makes re-extraction
   idempotent), `documents.page_count`/`has_text_layer`/`needs_ocr`
   aggregated from the pages, `extraction_status="completed"`.
5. Email attachments (case 4 only, when present) → each is ingested as an
   independent child `Document` via the normal `ingest_document()` path,
   then recursively extracted through this same function, depth-capped at
   5 to bound recursion on adversarial/accidental nesting. A byte-identical
   duplicate attachment is skipped, not treated as an error.

Every path (1–4) ends in exactly one `document_custody_events` row —
`extracted`, `re_extracted` (if `extraction_status` was not `pending`
before this call), or `extraction_failed`.

## 2. Final database schema additions

Confirmed by dumping the actual `CREATE TABLE` statements from a fresh
migration run this session:

**`documents` gained** (all additive, via migration `15160ee5686e`):
`page_count` (nullable int), `has_text_layer` (nullable bool), `needs_ocr`
(bool, `NOT NULL DEFAULT 0`), `ocr_status` (nullable, reserved for Phase 3
— never set to anything but null by this code), `extraction_status`
(`NOT NULL DEFAULT 'pending'`), `extraction_error` (nullable text).

**`document_pages`** (new table): `page_id` PK, `document_id` FK →
`documents`, `page_number`, `extracted_text` (nullable), `ocr_text`
(nullable, reserved for Phase 3, never written here), `extraction_method`
(`native`/`ocr`/`none`), `extraction_confidence` (nullable float, always
null in Step 1), `char_count`, `needs_ocr`, `source_sha256` (`NOT NULL`).
Unique constraint on `(document_id, page_number)`.

**`citations`** (new table): `citation_id` PK, `document_id` FK,
`page_id` FK → `document_pages` (nullable), `start_offset`, `end_offset`,
`paragraph_index`, `bounding_box` (JSON), `quoted_text` (`NOT NULL`).
Matches the design in `docs/DATA_MODEL.md` exactly — no deviation.

Nothing from Phase 1 or Step 0 was altered or dropped; every change is a
new column or new table.

## 3. Integrity requirements — re-verified, not just re-asserted

Checked directly against the current code this session:

- **§12.1 (status visible, not hidden):** `extraction_status` is read (never
  written) from six template locations across both the case list and the
  document detail page — confirmed by grep. There is no code path that
  determines status implicitly from row absence; the UI always reads the
  explicit column.
- **§12.2 (page-to-hash linkage):** `document_pages.source_sha256` has
  exactly one assignment site in the entire codebase —
  `app/core/extraction/service.py:145`, set to `document.sha256_hash` —
  confirming it's a real snapshot at write time, not a placeholder.
- **§12.3 (originals never modified):** grepped every extraction module for
  any write-mode file operation. The only match, `tmp_path.write_bytes(...)`
  in `service.py`, writes an **email attachment's bytes to a brand-new temp
  file** (`tmp_dir / attachment.filename`) — the same pattern the API layer
  already uses for uploads — which is then handed to `ingest_document()` to
  be copied into the vault normally. No extraction code opens a *stored*
  original in write mode anywhere. Backed by
  `test_extraction_never_modifies_the_stored_original`, which compares hash
  and raw bytes before/after, and by a live manual check this session
  (md5sum of the downloaded file matched the source file exactly after
  extraction ran).
- **§12.4 (citations as sole reference mechanism):** grepped for every
  `Citation(` construction site in the codebase — the only one outside the
  model definition itself is in the test suite
  (`test_citations_table_exists_and_is_not_written_by_extraction`), which
  exists specifically to prove the table is usable, not to make it part of
  any real workflow yet. No application code writes to `citations` in
  Step 1, exactly as planned — Step 4's highlight creation remains the
  first real writer.

## 4. Migration upgrade path from Phase 1 / Step 0

Confirmed via Alembic's own revision graph this session — a single linear
chain, no branches:

```
15aec3ba1e28  initial schema                         (Phase 1)
      ↓
79c226771ac3  add document date range end             (Step 0)
      ↓
15160ee5686e  add extraction core: document_pages/citations  (Step 1)
```

`run_migrations()` applies whichever of these a given vault hasn't seen
yet, in order, and is idempotent (re-running against an up-to-date vault
is a no-op) — this is the same mechanism every prior phase has used, no
new upgrade machinery introduced.

This path was specifically stress-tested this session, not just assumed:
`test_migrations_apply_cleanly_against_a_populated_documents_table` starts
a database at exactly the Step 0 revision, inserts a real document row
(simulating an existing vault), then upgrades the rest of the way to
head — the scenario that caught the `server_default` bug during
development. It passes.

## 5. Confirmation: no Step 2 features started

Re-checked this session, not carried forward from the implementation
turn's own claim:

- No FTS5 virtual table or trigger exists anywhere (`grep` for `FTS5`,
  `CREATE VIRTUAL`, `virtual table` — no matches in `app/`).
- No `tags` or `document_tags` table exists in the schema dump above.
- No `annotations` or `annotation_types` table exists in the schema dump
  above.
- No OCR execution code (`tesseract`, `pytesseract`) or AI/LLM code
  (`openai`, `anthropic`, `gpt`, `llm`, `summariz`) anywhere in `app/`.
- `git status` is clean and everything is pushed as of commit `0e76cd4` —
  no uncommitted or unpushed work exists that could represent
  started-but-unreported Step 2 work.
- Full test suite: **131 passed, 1 skipped** (the same pre-existing
  root-only permission skip from Phase 1), confirmed by a fresh run this
  session.

---

Step 1 is frozen as described above. Holding here — no Step 2
(search/indexing) work will begin until you approve proceeding.
