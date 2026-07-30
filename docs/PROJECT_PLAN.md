# FERPA Evidence Manager — Project Plan (Phase 1)

Status: **Draft for review — waiting on approval before any application code
is written.**

## Roadmap

**Phase 0 — Architecture & Planning (this deliverable)**
ARCHITECTURE.md, DATA_MODEL.md, PRIVACY_SECURITY.md, PROJECT_PLAN.md.
No code.

**Phase 1 — Foundations**
Repo scaffolding (`pyproject.toml`, app skeleton per ARCHITECTURE.md folder
structure); vault creation/config (`init_vault.py`, startup guardrail
against vault-inside-git-repo); DB schema + first Alembic migration; case
CRUD; basic ingestion (copy-in, SHA-256 hashing, read-only flag, dedup by
hash); minimal web UI to create a case and list/view ingested documents.
*Exit criteria: you can create a case, drop in files, and see them listed
with correct hashes and read-only originals on disk.*

**Phase 2 — Extraction & Search**
Per-format text extractors (PDF, DOCX, plain text, email); page-level
storage with offsets; low-text-yield heuristic → `needs_ocr` flagging;
FTS5 full-text search UI (filter by case/date/tag/record type/needs-OCR).
*Exit criteria: search returns real results across ingested documents, and
scanned/image-only files are correctly flagged as needing OCR.*

**Phase 3 — OCR**
Tesseract integration; OCR job queue + background worker; OCR review UI
(confidence display, side-by-side original vs. OCR text, manual correction
layer that never overwrites raw OCR output); reprocessing on demand.
*Exit criteria: a flagged scanned document can be OCR'd, reviewed, and
corrected without ever losing the original OCR output or touching the source file.*

**Phase 4 — Timeline**
Timeline event model; UI for attaching a citation (document + page + span)
to a date; date-extraction suggestions (user-confirmed, never auto-committed);
timeline view with filter/sort; visual surfacing of date gaps.
*Exit criteria: every timeline event on screen can be traced, in one click,
back to the exact page/excerpt it's based on.*

**Phase 5 — Missing / Conflicting Records**
`record_requirements` checklist entity + UI (user/attorney-defined, not
built-in legal rules); outstanding-requirement view; conflict-flagging
workflow with side-by-side citation comparison.
*Exit criteria: a user can define "expected records" and see which are
unmet, and can flag two excerpts as conflicting with a note.*

**Phase 6 — Evidence Binder**
Binder HTML/CSS template; PDF assembly via WeasyPrint + PyMuPDF (cover, TOC,
exhibit list, cited timeline, appended source documents); `binder_exports`
tracking with output hash for reproducibility.
*Exit criteria: a generated PDF binder where every citation can be checked
against an appended source page, and the export is reproducible from its
recorded document/event set.*

**Phase 7 — Hardening**
Full audit-log coverage review; one-click vault backup/export; at-rest
encryption implementation per the decision made before Phase 1 begins (see
Decision #2 below); cross-platform packaging polish (`start.sh`/`start.bat`
tested on both Windows and macOS); documentation pass; test coverage pass;
a focused security/privacy self-review against PRIVACY_SECURITY.md.

**Phase 8 — Optional / Stretch (explicitly opt-in, off by default)**
Local-only NLP assistance (e.g., a small on-device model for date/entity
suggestions) — only pursued after core trust and traceability are
established in Phases 1–7, and only if it can preserve the "nothing leaves
the device" guarantee without exception.

## Decisions to make before Phase 1 coding starts

These aren't blocking *this* document, but they materially affect early
implementation choices (especially #1 and #2, which are much cheaper to
decide now than to change after real case data exists):

1. **Default vault path.** Proposed default: `~/FERPA-Evidence-Vault/`,
   user-configurable at first run. Confirm or propose an alternative.

2. **At-rest encryption for v1.** Options: (a) rely on OS full-disk
   encryption only, keep plain SQLite for now, revisit later; (b) build on
   SQLCipher from the start so the DB is encrypted regardless of disk
   encryption state. (b) is more defensible for evidentiary material about a
   minor but adds passphrase/key-management UX that needs its own design
   (recovery story if the passphrase is forgotten). Recommend deciding
   before Phase 1's DB layer is built.

3. **Legal-content boundary.** Confirmed direction: no built-in legal
   deadlines/rules; `record_requirements` is a user/attorney-populated
   checklist, and gap/conflict language stays neutral (see
   PRIVACY_SECURITY.md §9). Flagging here for explicit sign-off since it
   shapes UI copy across every phase.

4. **File format coverage for v1.** Proposed v1 set: PDF, DOCX, plain
   text/RTF, common images (JPG/PNG/TIFF), email (.eml). Proposed
   deferred-to-later: legacy `.doc`, Outlook `.msg`, spreadsheets. Confirm or
   adjust based on what your actual record set looks like.

5. **Duplicate/near-duplicate policy.** Exact-hash duplicates are
   auto-detected and flagged (never auto-deleted). Near-duplicates (e.g., a
   letter scanned twice, or redacted vs. unredacted versions) are out of
   scope for automatic detection in v1 — flagged only if the user tags them.
   Confirm this is acceptable for v1.

6. **Expected document volume.** Roughly how many documents/pages are you
   anticipating (dozens, hundreds, low thousands)? This affects how much
   effort Phase 2/3 should put into indexing and OCR-queue performance versus
   keeping things simple.

7. **Backup ownership.** Proposed: manual by default in early phases, with
   an explicit one-click "export vault backup" added in Phase 7. Confirm
   this timing is acceptable, or whether backup should move earlier.

8. **Binder output format.** Proposed: PDF-only for v1 (standard for
   exhibit binders, page-stable for citations). DOCX/HTML export would be a
   later addition if needed. Confirm.

9. **Packaging trust.** `start.sh`/`start.bat` will run `pip install` from
   PyPI against a pinned `requirements`/`pyproject.toml` file — nothing
   downloaded outside of that. Worth you knowing this before running it, so
   you can audit the dependency list first if you want to.

## Risks

- **OCR accuracy risk.** Tesseract is good but not perfect on messy scans;
  the design mitigates this by always labeling OCR text distinctly with a
  confidence score rather than presenting it as verified text (see
  PRIVACY_SECURITY.md §3), but review workflow quality in Phase 3 matters a lot.
- **Date-extraction risk.** Automated date suggestions for timeline events
  can be wrong (e.g., a letter referencing a past date). Mitigated by always
  requiring user confirmation before an event is "confirmed" — see Phase 4.
- **Scope creep toward "legal judgment."** The gap/conflict features are
  easy to accidentally over-claim ("this app tells you what's missing").
  Mitigated by the user/attorney-defined-checklist design (Decision #3) and
  the UI language discipline in PRIVACY_SECURITY.md §9, but worth
  re-checking at each phase review.
- **Encryption-at-rest decision deferred too long.** If Decision #2 isn't
  made before Phase 1, the DB layer gets built against plain SQLite and a
  later switch to SQLCipher means migrating real case data, which is riskier
  than deciding up front.
- **Packaging friction on macOS Gatekeeper / Windows SmartScreen** if this
  ever moves from "start script" to "real installer" in a later phase —
  not a v1 concern given your answer, but worth flagging for Phase 7 planning.
