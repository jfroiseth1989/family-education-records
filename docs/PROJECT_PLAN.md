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
against vault-inside-git-repo); DB schema + first Alembic migration
(including `document_version_groups` and `document_custody_events` from the
start, since chain-of-custody begins at import — not deferred to a later
phase); case CRUD; basic ingestion (copy-in, SHA-256 hashing, read-only
flag, dedup by hash, `imported` custody event written in the same
transaction as the document row); a "link as new version" action from a
document's detail view; minimal web UI to create a case, list/view ingested
documents, view a document's custody history, and see version history where
applicable.
*Exit criteria: you can create a case, drop in files, and see them listed
with correct hashes and read-only originals on disk; every document shows
its custody log from the moment of import; importing a corrected version of
an existing document preserves the original untouched and clearly marks
which one is current.*

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

**Phase 3.5 — Fact & Observation Layer**
`verified_facts` / `ai_observations` / `ai_summaries` tables and the
promotion workflow (review an observation → accept → new verified fact with
lineage, or reject); mandatory confidence input on every verified fact;
mandatory fixed label rendering on every AI summary. This sits between OCR
(Phase 3) and Timeline (Phase 4) because the timeline is now built on
verified facts, not raw citations.
*Exit criteria: an AI-suggested date or name can be reviewed and either
promoted to a verified fact (with visible lineage back to the suggestion) or
rejected; nothing machine-generated is visible outside a clearly labeled
"pending review" state until a human acts on it.*

**Phase 4 — Timeline**
Timeline event model built on `verified_facts` (via `timeline_event_facts`);
UI for attaching a verified fact — or creating one directly from a citation —
to a date; date-extraction suggestions flow through the Phase 3.5 review gate
before they can be attached to an event; timeline view with filter/sort;
visual surfacing of date gaps.
*Exit criteria: every timeline event on screen can be traced, in one click,
back to the exact page/excerpt and confidence level it's based on, and no
unreviewed AI suggestion can reach the timeline directly.*

**Phase 5 — Missing / Conflicting Records**
`record_requirements` checklist entity + UI (user/attorney-defined, not
built-in legal rules); outstanding-requirement view; conflict-flagging
workflow built on `verified_facts` with side-by-side comparison (including
each side's confidence level).
*Exit criteria: a user can define "expected records" and see which are
unmet, and can flag two verified facts as conflicting with a note.*

**Phase 6 — Evidence Binder**
Binder HTML/CSS template; PDF assembly via WeasyPrint + PyMuPDF (cover, TOC,
exhibit list, cited timeline, appended source documents); per-section
provenance (`binder_sections` / `binder_export_sources`) recorded for every
generated binder; generation logic that structurally confines any included
AI summary to its own labeled appendix section, never the narrative/exhibit/
timeline sections; `binder_exports` tracking with output hash for
reproducibility.
*Exit criteria: a generated PDF binder where every citation and every
timeline entry can be checked against an appended source page and its
recorded provenance, any included AI summary is unmistakably labeled and
segregated in its own appendix, and the export is reproducible from its
recorded section/source manifest.*

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

10. **Confidence scale definition.** Proposed default: a required 3-value
    `confidence_label` (`certain` / `probable` / `uncertain`) on every
    verified fact, plus an optional numeric `confidence_score` (0.0–1.0)
    populated when the fact came from a scored method (OCR, date-parser).
    Confirm this is the right scale, or specify a different one (e.g. a
    5-point scale, or numeric-only) — this affects UI design in Phase 3.5
    and is worth locking in before that phase starts.

11. **What counts as an "AI observation" in v1.** Given no cloud AI is used
    (per PRIVACY_SECURITY.md), Phase 3.5's `ai_observations` in v1 would
    realistically be populated by: OCR text (confidence from Tesseract),
    regex/heuristic date-parsing, and simple heuristic name/entity matching
    against the `people` table — not a general-purpose LLM. Confirm that's
    the intended v1 scope for "AI-generated," versus deferring some of these
    (e.g. date-parsing) to be treated as deterministic/native extraction
    rather than routed through the observation-review workflow at all. This
    changes how much review-queue UI Phase 3.5 actually needs for v1.

12. **Version-linking UX.** Confirmed direction: linking two documents as
    versions of each other is always a manual, user-driven action (see
    DATA_MODEL.md `document_version_groups`) — the app does not auto-detect
    "this looks like a new version of that." Worth deciding now whether v1
    should also offer a *suggestion* (e.g., "this new import has the same
    document type and a similar filename/date to an existing document —
    link as a new version?") as a Phase 3.5-style `ai_observations` row the
    user can accept or dismiss, or whether that's unnecessary complexity for
    v1 and purely manual linking is enough to start.

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
- **Review-queue fatigue.** If every OCR'd word or date-parse hit becomes an
  `ai_observations` row awaiting review, the review queue could get large
  enough that a user starts bulk-accepting without really checking — which
  would defeat the point of the human-review gate. Mitigated by keeping the
  observation-worthy threshold deliberately narrow in Phase 3.5 (see open
  decision #11) rather than routing every low-stakes extraction through the
  same queue as a suggested date or name.
