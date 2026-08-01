# Phase 1 Freeze Summary

Status: **Phase 1 locked.** Approved by owner (case management, ingestion,
versioning, custody — commit `0aa54db`; document date workflow — commit
`8038a6c`). This document is the final foundation record before Phase 2
planning begins. No Phase 2 code exists as of this freeze.

Branch: `claude/ferpa-evidence-architecture-awlfqt`. Every claim below was
re-verified directly against the repository at the time this document was
written, not carried forward from memory of earlier turns.

## 1. Locked Phase 1 scope

Everything below is implemented, tested, and now frozen — changing any of
it is a deliberate decision going forward, not a Phase 2 side effect.

| Area | What's locked |
|---|---|
| **Vault lifecycle** | `app/core/vault.py` — vault directory creation, `vault.json` metadata, and the hard guardrail refusing to initialize/use a vault path inside any git working tree. |
| **Schema management** | 100% Alembic-driven (`app/db/migrate.py`); no `create_all` path exists anywhere, including in tests. One migration on record: `15aec3ba1e28_initial_schema`. |
| **Case management** | Create, list, view, edit (label/description/status). No delete route — retirement is via `status`, not removal (see Risk #2 below). Every create/edit writes an `AuditLog` row. |
| **Document ingestion** | Copy-in (never move/link the source), SHA-256 hashing, read-only storage (`-r--------`, content-addressed under `originals/<hash>/<filename>`), exact-duplicate rejection per case, `imported` custody event written atomically with the document row. |
| **Document dates** | `document_date` / `_precision` (`exact`/`approximate`) / `_source` (`manual` now; `extracted`/`file_metadata` reserved). Optional — unknown is a valid, first-class state, never guessed or backfilled from filesystem metadata. Entered fresh per version, never inherited. |
| **Document versioning** | A corrected/reissued file is ingested as an independent row and explicitly linked via `document_version_groups` / `supersedes_document_id`. "Exactly one current version per group" enforced by a **database-level** partial unique index, not just application logic. |
| **Chain of custody** | Append-only `document_custody_events` per document (`imported`, `version_linked`, `version_superseded`, `hash_verified`, ...), each snapshotting hash/size/location at that event. Manual "Verify integrity now" action, always logs the outcome whether it matches or not. |
| **Web UI** | Server-rendered (Jinja2 + plain HTML forms), case list/detail, document detail (custody log, version history, date), upload and new-version forms. No external CDN assets. |
| **Local-first posture** | Binds `127.0.0.1` only (`app/config.py`). No outbound-network code, no telemetry, in any runtime dependency or app code (re-verified this session — see §4). |
| **Tests** | 65 collected, 64 passing, 1 skipped (root-only permission-bypass case, not a real gap on a normal user account). |
| **Run tooling** | `scripts/start.sh` / `start.bat` / `init_vault.py`, README run/test instructions. |

## 2. All tests passing — confirmed this session

```
$ .venv/bin/pytest -q
.....................................s...........................  [100%]
64 passed, 1 skipped, 1 warning in 7.33s
```

10 test files, 65 collected tests total:
`test_vault.py`, `test_files.py`, `test_ingestion.py`, `test_versioning.py`,
`test_custody.py`, `test_document_dates.py`, `test_migrate.py`,
`test_seed.py`, `test_api_cases.py`, `test_api_documents.py`.

The single skip is `test_make_read_only_prevents_writes_for_non_root_user`
— it's gated off specifically because this session runs as root, which
bypasses POSIX file-permission bits by design of the OS, not a defect in
the app. The portable half of that guarantee
(`test_make_read_only_clears_write_permission_bits`) does run and pass.

## 3. Final database/schema state — introspected directly, not recalled

Ran a fresh `run_migrations()` against an empty database and dumped the
resulting schema this session. Seven tables plus Alembic's own bookkeeping
table:

- **`cases`** — case_id, label, description, status, created_at
- **`document_types`** — lookup table (type_id, name *unique*, description,
  is_active), seeded with 11 defaults (IEP, 504 Plan, Evaluation,
  Correspondence, Discipline, Attendance, Grades, Medical, Legal Filing,
  Audio Transcript, Other)
- **`document_version_groups`** — group_id, case_id (FK), label,
  created_at, current_document_id (no FK — deliberate, see
  `DocumentVersionGroup` docstring: SQLite can't add a circular FK via
  ALTER TABLE, so this pointer is kept correct by application logic under
  test, not a DB constraint)
- **`documents`** — full column set: identity/hash/storage (`original_filename`,
  `stored_path`, `sha256_hash` *indexed*, `mime_type`, `file_size_bytes`),
  categorization (`source`, `document_type_id` FK), the date fields
  (`document_date`, `document_date_precision`, `document_date_source`),
  ingestion metadata (`ingested_at`, `ingested_by`, `notes`, `deleted_at`),
  and versioning (`version_group_id` FK, `version_number`,
  `is_current_version`, `supersedes_document_id` FK, `version_note`).
  Constraints: `UNIQUE(case_id, sha256_hash)` and the partial unique index
  `uq_one_current_version_per_group ON documents(version_group_id) WHERE
  is_current_version = 1`.
- **`document_custody_events`** — custody_event_id, document_id (FK),
  event_type, event_timestamp, actor, plus the per-event snapshot columns
  (`original_filename`, `sha256_hash_at_event`, `file_size_bytes_at_event`,
  `storage_location_at_event`), `details` (JSON)
- **`audit_log`** — log_id, case_id (FK, nullable), event_type,
  entity_type, entity_id, actor, timestamp, details (JSON)
- **`alembic_version`** — Alembic's own migration-tracking table

**No tables exist yet for**: `document_pages`, `citations`, `verified_facts`,
`ai_observations`, `ai_summaries`, `timeline_events`, `conflicts`,
`record_requirements`, the relationship-graph tables, or `annotations`.
Their design is finalized in `docs/DATA_MODEL.md`; none are built. This is
expected — they belong to Phase 2 onward.

## 4. Confirmation: no Phase 2 features implemented

Re-checked directly this session, not asserted from memory:

- Grepped all `.py`/`.html`/`.toml`/`.ini` source for OCR, extraction, and
  AI-related terms (`tesseract`, `pymupdf`/`fitz`, `python-docx`, `fts5`,
  `openai`/`anthropic`/`gpt`/`llm`, `summariz`, etc.) — zero matches outside
  three pre-existing docstring sentences that merely *name* later phases in
  passing ("in later phases: extraction, OCR, ...").
- `pyproject.toml` dependencies are unchanged from the original Phase 1
  set: FastAPI, Uvicorn, SQLAlchemy, Alembic, Jinja2, python-multipart,
  pydantic-settings. No PDF/OCR/NLP/HTTP-client library has been added.
- `app/` contains no `extraction/`, `ocr/`, `indexing/`, `timeline/`, or
  `binder/` modules — only `api/`, `core/` (vault, files, custody,
  document_dates, ingestion), `db/`, and `web/`, matching exactly the
  Phase 1 subset of the folder structure approved in `docs/ARCHITECTURE.md`.
- No `document_pages`/`citations`/fact/timeline/relationship/annotation
  tables exist in the schema (§3).

## 5. Risks, limitations, and decisions to resolve before Phase 2 planning

Carried forward from `docs/PHASE_1_REVIEW.md` where still open, re-checked
against the current state, plus one new item from the date-workflow change.

1. **Vault default location.** Still `~/FERPA-Evidence-Vault`, unconfirmed
   as your final choice. *Open.*
2. **Case deletion.** Still no delete route by design — "archived" status
   is the only retirement path. *Open — confirm this is acceptable
   permanently, since Phase 2+ features (search, timeline) will assume
   cases persist rather than disappear.*
3. **Integrity re-verification is manual-only.** No scheduled/automatic
   hash re-check exists. *Open.*
4. **Version-linking is manual-only.** No "looks like a version of..."
   suggestion exists or is planned before Phase 8. *Open.*
5. **Document date entry is manual-only, resolved for now.** The gap
   flagged in the original review is closed — dates can be entered, left
   unknown, marked approximate, and are never conflated with filesystem
   timestamps. Remaining sub-decision: `DocumentDatePrecision` currently
   supports `exact`/`approximate` only, not `range` (a range would need a
   second column, deliberately deferred). *Confirm this limitation is
   acceptable, or scope a range-date column now before Phase 2's date
   *extraction* work potentially needs to represent a range it infers from
   text (e.g. "sometime in March 2024").*
6. **`document_version_groups.current_document_id` has no database-level
   foreign key** (SQLite can't add one via ALTER TABLE without breaking the
   circular reference — documented trade-off, covered by tests, but still
   an application-enforced invariant rather than a DB-enforced one, unlike
   the "one current version per group" rule which *is* DB-enforced).
   *Informational — no action proposed, just flagging the asymmetry.*
7. **New: schema readiness for Phase 2's citation model.** `docs/DATA_MODEL.md`
   already specifies `document_pages` and `citations` as the traceability
   backbone for Phase 2. Confirm before Phase 2 planning starts that
   nothing in the frozen Phase 1 schema needs to change to accommodate
   them — based on this session's review, nothing does (they're purely
   additive new tables), but this is the last checkpoint to raise it before
   Phase 2 migrations are written.

None of the above block Phase 2 technically. They're either accepted
Phase 1 trade-offs worth a final conscious sign-off, or one substantive
question (#5) worth resolving before Phase 2's date-extraction work makes
assumptions about the precision model.

## Post-freeze addendum: owner decisions

All seven items above were reviewed and resolved by the owner:

1. **Vault location** — `~/FERPA-Evidence-Vault` confirmed as the
   permanent default; `FERPA_VAULT_PATH` stays configurable (already true —
   no change needed).
2. **Case retirement** — no-delete-only-archive confirmed as the
   *permanent* model, not a Phase 1 placeholder. Evidence records are never
   permanently deletable through the application, now or in any later phase.
3. **Integrity re-verification** — stays manual-only. Automatic/scheduled
   re-verification is explicitly deferred pending a clear workflow reason
   and its own design review — not to be added incidentally by a later phase.
4. **Version linking** — manual-only confirmed as permanent. No automatic
   superseded-relationship inference, ever, without user confirmation.
5. **Date precision** — resolved by expanding the design (not by further
   Phase 1 code changes): `docs/DATA_MODEL.md` now specifies a
   `range` precision value backed by a `*_date_range_end` column, as a
   reusable pattern applying to `documents.document_date` today and
   `timeline_events.event_date` when Phase 4 builds it. Implementation
   (an additive migration to `documents` plus a small UI/service update)
   is scoped as the first work item of the Phase 2 plan, not implemented
   yet — see `docs/PHASE_2_PLAN.md`.
6. **`current_document_id` FK limitation** — accepted as documented. No
   change.
7. **`document_pages`/`citations` design** — confirmed to proceed as
   already approved, unless Phase 2 planning surfaces a genuine
   architectural conflict (none identified).

Phase 1 remains frozen as described above; item 5's *design* is now final,
its *implementation* is queued as Phase 2 work pending plan approval.
