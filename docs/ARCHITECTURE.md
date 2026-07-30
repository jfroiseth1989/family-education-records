# FERPA Evidence Manager — Architecture (Phase 1)

Status: **Draft for review — no application code written yet.**

## 1. Purpose and Framing

This is a private, local-first tool that helps a parent/guardian organize a
child's educational records into a searchable, chronologically ordered,
fully-cited evidence set. It is a **software architecture and record-keeping
tool**, not a legal-advice tool. It never characterizes anything as a "FERPA
violation," never asserts legal conclusions, and never invents deadlines —
it surfaces gaps and conflicts for the user (or their attorney) to interpret.

Non-goals for v1:
- Not a case-management or e-filing system.
- Not multi-user / collaborative (single local user).
- Not a compliance auditor — no baked-in legal rules.
- Not a cloud service. There is no server component outside the user's machine.

## 2. High-Level Architecture

A single local Python process serves both the API and the browser UI,
bound only to `127.0.0.1`. All state lives in a "vault" directory that is
**physically separate from the git repository** (see §4). Nothing in this
architecture makes an outbound network call by default.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser (http://127.0.0.1:<port> — never 0.0.0.0, never public)     │
│  Jinja2 + htmx + vanilla JS + self-hosted PDF.js (no CDN, no fonts    │
│  or scripts loaded from the internet)                                │
└───────────────────────────────▲───────────────────────────────────--┘
                                 │ localhost only
┌───────────────────────────────┴──────────────────────────────────────┐
│                        FastAPI application (uvicorn)                  │
│                                                                        │
│  ┌───────────────┐  ┌────────────────┐  ┌───────────────────────┐    │
│  │  Case / Doc    │  │  Search API    │  │  Timeline / Binder     │   │
│  │  API routers   │  │  (FTS5)        │  │  API routers           │   │
│  └───────┬────────┘  └───────┬────────┘  └──────────┬────────────┘   │
│          │                   │                       │                │
│  ┌───────▼───────────────────▼───────────────────────▼────────────┐  │
│  │                     Core domain services                        │  │
│  │  ingestion │ versioning │ extraction │ ocr │ indexing │          │  │
│  │  annotations │ fact/relationship layer (human-review gate) │     │  │
│  │  timeline │ binder                                              │  │
│  └───────┬────────────────────────────────────────────┬───────────┘  │
│          │                                             │              │
│  ┌───────▼────────┐                          ┌─────────▼──────────┐  │
│  │ Background job  │                          │  SQLAlchemy models  │  │
│  │ worker (thread   │                          │  + Alembic          │  │
│  │ pool, in-process)│                          │  migrations          │  │
│  └────────────────┘                          └──────────┬───────────┘  │
└──────────────────────────────────────────────────────────┼─────────────┘
                                                             │
                                          ┌──────────────────▼──────────────────┐
                                          │      Vault (outside git repo)        │
                                          │  db.sqlite (metadata + FTS5 index)   │
                                          │  originals/  (read-only, untouched)  │
                                          │  derived/    (extracted/OCR text)    │
                                          │  exports/    (generated binders)     │
                                          │  logs/audit.log (append-only)        │
                                          └───────────────────────────────────────┘
```

## 3. Core Components

### 3.1 Ingestion
- User adds files (drag-and-drop or "Add from folder") into a case.
- The app **copies** the file into `originals/`, never moves/links in a way
  that lets the app write back to the source location.
- Computes SHA-256 immediately; records size, mime type, ingestion timestamp.
- Sets the copied file read-only at the filesystem level where the OS supports it.
- Detects exact duplicates by hash (flags, does not silently discard).
- Writes the document's `imported` `document_custody_events` row in the same
  transaction as the `documents` row — filename, hash, size, and storage
  location are recorded as part of the document coming into existence, not
  as a follow-up step that could be skipped.
- Optionally, the user marks the new file as a new (or prior) version of an
  existing document — see §3.2.

### 3.2 Document Versioning & Chain of Custody

**Versioning.** A corrected record or a reissued IEP is never imported as an
edit to the existing document — it's ingested as an ordinary new document
(own hash, own file, own row), which the user then explicitly links to the
earlier one as a new version. Linking two documents this way:
- creates a `document_version_groups` row on first use (or reuses the
  existing group if the earlier document already has one),
- sets `supersedes_document_id` on the new row to point at the version it
  replaces,
- flips `is_current_version` to `false` on the previous current version and
  `true` on the new one, and updates the group's `current_document_id`, all
  in one transaction,
- appends `version_linked` / `version_superseded` custody events on both
  documents.

Every previous version remains fully intact on disk and in the database —
"current" is a label on a relationship, not a state that erases anything.
Browsing a case shows the current version by default; a superseded
version's detail view is clearly marked "Superseded by [version] — view
current," and the current version's view links back through its version
history. Facts and citations that reference a superseded version's pages
keep working (that historical page is exactly what they cite) but the UI
flags them as "based on a superseded version" so the user can decide
whether to also cite the current version.

**Chain of custody.** Every document accumulates an append-only
`document_custody_events` trail from the moment it's imported: the initial
`imported` event, then one row for every subsequent action — extraction,
OCR, tagging, linking to a fact, version linking, inclusion in a binder
export. Each row independently records the hash, size, and storage location
*as observed at that event*, not just once at ingestion — so the ledger
itself shows hash consistency over the document's life rather than relying
on a single check. This is intentionally a dedicated, structured table
(§ see DATA_MODEL.md `document_custody_events`) rather than reuse of the
general `audit_log`, since a custody record needs those fields to always be
present, not optionally buried in a JSON blob. A document's full custody
history is viewable from its detail page and can be included as an appendix
in a generated binder.

### 3.3 Extraction
- Format-specific extractors run per document, producing page/paragraph-level
  text plus offsets:
  - PDF → native text layer + per-page word bounding boxes (PyMuPDF).
  - DOCX → paragraph-indexed text (python-docx).
  - Email (.eml) → headers + body, attachments treated as child documents.
  - Plain text/RTF → direct read.
  - Images (JPG/PNG/TIFF) → no native text; routed straight to OCR queue.
- Each page/paragraph gets a low-text-yield heuristic check (e.g., fewer than
  ~10 extractable words on a page that contains an image) → flagged
  `needs_ocr = true`. This is how "identify files that need OCR" is implemented:
  deterministically, not by guessing file type alone.

### 3.4 OCR
- Tesseract OCR (fully offline, no cloud vision API) via `pytesseract`,
  rendering pages to images with PyMuPDF/`pdf2image`.
- Runs as background jobs (`ocr_jobs` table), so large scans don't block the UI.
- OCR output is stored **separately** from native-extracted text and always
  labeled with `extraction_method = ocr` and a confidence score. OCR text is
  never silently merged into "clean" extracted text — evidentiary reliability
  depends on knowing which text came from a machine guess versus the
  document's actual text layer.
- Manual corrections to OCR text are stored as an annotation layer on top of
  the original OCR output, not as an overwrite — the raw OCR output is retained.

### 3.5 Indexing / Search
- SQLite FTS5 virtual table over page-level extracted/OCR text.
- Search UI supports filtering by case, date range, tag, person, record type,
  and needs-OCR status.

### 3.6 Annotation Layer

Highlights, bookmarks, and notes are how a user works with a document
without ever touching it. All three are rows in `annotations` referencing a
document and, where relevant, a page or an exact citation span — nothing
about them is written into the source file.
- Rendering is overlay-based: the self-hosted PDF.js viewer draws highlight
  boxes and bookmark markers on top of the rendered page at view time, from
  the annotation's stored coordinates/span, the same way any PDF annotation
  tool works without mutating the underlying file.
- Notes have their own full-text index (`annotation_notes_fts`), kept
  separate from `document_text_fts` — a search result is always clearly
  either "found in the source document" or "found in your notes about it,"
  never ambiguous between the two.
- Annotations are personal working notes, not evidence: they are excluded
  from binder generation by default and can never stand in as a citation
  source for a `verified_fact` or `verified_relationship`. If the substance
  of a note matters to the case, the workflow is to turn it into an actual
  fact with its own citation — same bar as anything else the app treats as
  established.

### 3.7 Fact, Observation & Summary Layer

This layer sits between extraction/OCR and everything that consumes their
output (timeline, conflicts, binder), and is what makes the following
guarantees structural rather than aspirational:

- **Every fact-like record carries a confidence level and at least one
  citation** — no code path in extraction, OCR, or date-parsing produces a
  bare, unattributed "fact." See `verified_facts`/`ai_observations` in
  DATA_MODEL.md, both of which require a citation and a confidence value.
- **Verified facts and AI-generated observations are separate tables, not a
  shared table with a status flag.** Anything machine-suggested (a
  date-parser hit, an OCR-derived name, a suggested category) lands in
  `ai_observations` with `status = pending_review` and a confidence score.
  It is **not visible to the timeline, conflict tracker, or binder
  narrative** — those only ever read `verified_facts`. A human reviewing an
  observation and accepting it causes the app to create a *new*
  `verified_facts` row that references the originating observation
  (`source_observation_id`) for lineage; the observation row itself is
  never edited or deleted, so the suggestion-to-confirmation history is
  always auditable.
- **AI-generated summaries (free-text) are a third, separate concept** —
  `ai_summaries` — and can never be promoted into `verified_facts` under any
  circumstance, reviewed or not. A summary is interpretive synthesis, not a
  discrete citable claim, so promoting it to "fact" status would misrepresent
  what it is. Every summary carries a fixed, app-enforced label ("AI-Generated
  Summary — Unverified, Requires Human Review. Not a Fact or Legal
  Conclusion.") that renders wherever the summary appears, on screen or in
  an exported binder, and defaults to `review_status = pending_review`.
- This layer is where the "not legal advice" boundary is enforced in code,
  not just in UI copy: nothing the app labels as a fact was asserted by the
  app itself — it was either directly cited from a source document
  (`verified_facts`, human-confirmed) or explicitly marked as an unreviewed
  machine guess (`ai_observations`, `ai_summaries`).

### 3.8 Timeline
- Timeline entries are **built from verified facts, not free text or raw
  AI output**: every event links to one or more `verified_facts` rows, each
  of which already carries its own citation(s) and confidence. The UI is
  built around "attach this confirmed fact to a date" rather than a
  free-text journal, so every timeline claim is traceable to an exact source
  location and carries a confidence level.
- Dates can be extracted-and-suggested (regex/date-parsing over text), but a
  suggestion is an `ai_observations` row until a human reviews and promotes
  it — it never appears on the timeline as a suggestion; it either isn't on
  the timeline yet, or it's a confirmed fact.

### 3.9 Relationship Graph

Connects people, organizations, documents, meetings, evaluations, IEPs,
emails, incidents, transportation decisions, providers, services, and
timeline events — using a small set of node types (`person`, `organization`,
`document`, `timeline_event`) rather than a table per noun, since a meeting
or incident is already a `timeline_event`, an IEP or evaluation is already a
`document`, and a provider is already a `person`/`organization` with that
role (see DATA_MODEL.md "Relationship Graph" for the full rationale).

- **Reuses the verified/AI-suggested split from §3.7, exactly.**
  `verified_relationships` is the only table a graph view renders as an
  established connection; `ai_suggested_relationships` sits in a pending
  queue and is never merged into the main graph until a human reviews and
  promotes it (which creates a new verified row with lineage back to the
  suggestion — the suggestion itself is retained, not overwritten). A
  suggested edge that's shown at all is visually distinct (e.g. dashed,
  labeled "Suggested — needs review").
- **Every edge — verified or suggested — must cite at least one source
  document.** There's no path to creating a relationship without at least
  one citation; the UI for drawing an edge requires selecting the
  document/page/excerpt that supports it.
- **v1 can be entirely manual and still fully satisfy the requirement.**
  "Never infer without marking AI-suggested and requiring approval" is
  satisfied trivially if there's no inference at all — the user (or their
  attorney) draws every edge by hand, each with its citation. An automatic
  suggestion engine (e.g., "these two people are named on the same
  document — possibly related?") is a separate, optional capability layered
  on top later; given this system has no cloud AI, any such engine would be
  a local heuristic, not a model call. See PROJECT_PLAN.md for whether
  that's in scope for v1.
- **Graph view.** A visual graph (nodes = entities, edges = relationships)
  filterable by entity type, relationship type, date range, and confidence;
  clicking an edge shows its citation(s) directly, consistent with every
  other traceability guarantee in this system. A tabular "relationships
  list" view covers the same data for anyone who prefers it to a node graph.
- **Known trade-off:** the `from_entity_type`/`to_entity_type` polymorphic
  reference can't be enforced as a database foreign key in SQLite. v1
  validates "the referenced entity actually exists" at the application
  layer, in the same transaction as the write. Flagged as a decision to
  confirm before Phase 1 (see PROJECT_PLAN.md) — the alternative is a
  heavier per-entity-type join-table scheme that trades simplicity for
  stronger DB-level guarantees.

### 3.10 Missing / Conflicting Records
- **Missing records**: a user- (or attorney-) defined checklist of expected
  records (`record_requirements`), e.g. "Annual IEP review," with an expected
  date/recurrence. The app flags checklist items with no linked document as
  outstanding. The app does **not** ship a built-in table of legal deadlines —
  that's a deliberate boundary given the "not legal advice" framing (see
  PROJECT_PLAN.md, decision #3).
- **Conflicting records**: a human-driven workflow, built on `verified_facts`
  (not raw citations) so a flagged conflict always shows each side's
  confidence level, not just competing excerpts. The user (or attorney)
  selects two or more verified facts across documents and flags them as
  conflicting, with a note. The UI supports side-by-side comparison. No
  automated contradiction detection in v1 (flagged as a stretch goal,
  local-only NLP, in PROJECT_PLAN.md) — and if added later, its output would
  land in `ai_observations` like any other machine suggestion, subject to
  the same human-review gate before it could ever become a flagged conflict.

### 3.11 Evidence Binder
- Assembles selected documents + timeline (verified facts) + citation index
  into a single paginated PDF: cover page, table of contents, exhibit list
  (numbered, matching source documents), chronological timeline with inline
  citations, and appended source documents (merged via PyMuPDF) so every
  citation in the binder can be checked against the actual page it cites.
- **Full provenance per section, not just per binder.** Every rendered
  section (`binder_sections`) records exactly which documents, verified
  facts, or citations contributed to it (`binder_export_sources`) — so a
  reader (or the user's attorney) can see precisely which source documents
  back a given timeline entry or exhibit, not just that "this binder
  included these documents somewhere."
- **AI-generated summaries are structurally confined to a separate,
  clearly-labeled appendix** (`section_type = summary_appendix`) — the
  binder-generation logic itself refuses to place `ai_summaries` content
  into exhibit, timeline, or narrative sections, so this separation is
  enforced by the generator, not left to template discipline. The mandatory
  label text renders on every such section regardless of review status.
- **Defaults to current versions.** Selecting a document for the binder
  pulls in whichever version has `is_current_version = true` unless the user
  deliberately chooses an older version (e.g., to show that a correction
  occurred); an included superseded version is rendered with a visible
  "SUPERSEDED — see current version" marker rather than presented as if it
  were the operative record.
- An optional chain-of-custody appendix section can render a document's full
  `document_custody_events` history alongside it, for cases where the
  custody trail itself is part of what needs to be shown.
- An optional relationships appendix can list selected `verified_relationships`
  with their citations (e.g. "who evaluated the student, when, resulting in
  which IEP") — never `ai_suggested_relationships`, which are excluded from
  binder generation entirely until promoted.
- Every export is recorded in `binder_exports` (hash of the resulting file,
  template version) so a binder can be regenerated or audited later —
  reproducibility matters for evidentiary use.
- Rendered with WeasyPrint (HTML/CSS → PDF) so the binder template reuses the
  same templating system as the web UI.

### 3.12 Extensibility (future document types & search capabilities)

Three schema-level choices exist specifically so later versions can add
document types, event/fact categories, or new search capabilities without a
redesign (full detail in DATA_MODEL.md "Extensibility"):

- `document_types`, `event_types`, and `fact_types` are lookup tables, not
  hardcoded enum columns — a new document type (e.g. audio transcripts,
  spreadsheets) is a row insert.
- `document_metadata` is a generic key/value table for attributes that only
  apply to some document types, so type-specific fields don't require
  nullable columns added to `documents` for every hypothetical future format.
- Search is layered on stable IDs (`page_id`, `citation_id`), so a future
  capability — e.g. local-only semantic/vector search — is an additive table
  referencing those IDs, not a change to the tables it indexes.

## 4. Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best-in-class local libraries for PDF/DOCX/OCR; no compiled toolchain needed; identical on Windows/macOS. |
| Web framework | FastAPI + Uvicorn | Typed, fast to develop, built-in OpenAPI docs (useful even for a solo dev), easy to bind strictly to `127.0.0.1`. |
| UI | Server-rendered Jinja2 + htmx + vanilla JS, self-hosted PDF.js | Avoids a JS build pipeline and, more importantly, avoids any CDN-hosted script/font/stylesheet — every asset ships in the repo so "nothing calls home" is auditable by reading `static/`. A React/Vite SPA is the natural upgrade path later if UI complexity grows (e.g., richer PDF annotation), but adds a build step and more third-party JS packages to vet for v1. |
| Database | SQLite + FTS5 | Zero-install, single-file, trivially backed up, portable between Windows/macOS. `SQLCipher` is the drop-in encrypted-at-rest alternative — see open decision #2 in PROJECT_PLAN.md; easier to decide *before* real data exists than to migrate later. |
| ORM/migrations | SQLAlchemy + Alembic | Explicit schema, versioned migrations as the data model evolves across phases. |
| PDF parsing/rendering | PyMuPDF (`fitz`) | Text + bounding boxes + page rendering + PDF merging (needed for both extraction and binder assembly) in one dependency. |
| DOCX parsing | python-docx | Paragraph-indexed text extraction for exact citations. |
| Email parsing | stdlib `email` (.eml), `extract-msg` (.msg, Phase 2+) | Offline, no network. |
| OCR | Tesseract via `pytesseract` | Mature, fully offline, no per-page cloud cost, no data leaves the device. |
| Binder PDF generation | WeasyPrint | HTML/CSS templating shared with the web UI; no cloud rendering service. |
| Background jobs | In-process `ThreadPoolExecutor` + a `jobs` table in SQLite | Single-user, single-machine tool — Celery/Redis would be operational overhead with no benefit here. |
| Packaging (v1) | `start.sh` / `start.bat` that create a venv, install pinned deps from PyPI, run the app, open the default browser | Matches the "comfortable with a start script" answer; avoids code-signing/installer work for v1. An installer (PyInstaller + signing) is a Phase 7 candidate, not a v1 requirement. |
| Testing | pytest | Standard, no extra infra. |

## 5. Folder Structure

Two physically separate trees — this separation is a security control, not
just tidiness (see PRIVACY_SECURITY.md §1):

**A. This git repository (source code only — safe to push to GitHub):**

```
family-education-records/
├── app/
│   ├── main.py                 # FastAPI app entrypoint, binds 127.0.0.1 only
│   ├── config.py               # vault path resolution, settings, .env loading
│   ├── api/                    # routers: cases, documents, search, timeline, binder, ocr
│   ├── core/
│   │   ├── ingestion/          # file intake, hashing, copy-in, dedup
│   │   ├── extraction/         # per-format text extractors
│   │   ├── ocr/                # OCR queue + tesseract wrapper
│   │   ├── indexing/           # FTS5 index management
│   │   ├── timeline/           # event builder, gap surfacing
│   │   └── binder/             # binder assembly/export
│   ├── db/
│   │   ├── models.py           # SQLAlchemy models
│   │   ├── migrations/         # Alembic
│   │   └── session.py
│   ├── web/
│   │   ├── templates/          # Jinja2
│   │   └── static/             # self-hosted JS/CSS/PDF.js — no CDN references
│   └── jobs/                   # background worker loop
├── tests/
├── scripts/
│   ├── start.sh / start.bat    # setup + run + open browser
│   └── init_vault.py           # create a new vault at a chosen path
├── docs/
│   ├── ARCHITECTURE.md         # this file
│   ├── DATA_MODEL.md
│   ├── PRIVACY_SECURITY.md
│   └── PROJECT_PLAN.md
├── pyproject.toml
├── .gitignore                  # excludes .env, *.sqlite, any local vault-shaped path (defense in depth)
└── README.md
```

**B. The vault (user's case data — never committed, never pushed, default
location *outside* the repo, e.g. `~/FERPA-Evidence-Vault/`):**

```
FERPA-Evidence-Vault/
├── vault.json                  # vault metadata: version, created_at, encryption flag
├── db.sqlite                   # cases, documents + versions, custody log, timeline, tags, audit log, FTS5 index
├── cases/
│   └── <case_id>-<slug>/
│       ├── case.json           # case metadata
│       ├── originals/          # read-only copies of ingested files, immutable
│       │   └── <hash-prefix>/<original_filename>
│       ├── derived/
│       │   ├── extracted_text/ # per-document extracted text + offsets (JSON)
│       │   ├── ocr_text/       # per-document OCR output + confidence
│       │   └── thumbnails/
│       └── exports/
│           └── binder_<timestamp>.pdf
├── logs/
│   └── audit.log               # append-only
└── backups/                    # optional manual/one-click backup snapshots
```

One `db.sqlite` per vault (not per case) — every table carries a `case_id`
foreign key. This keeps cross-case search and referential integrity simple
and means a single file backup captures everything, which matters more than
per-case DB isolation for a single-user tool. See PROJECT_PLAN.md open
decision list if this assumption should change (e.g., if cases need to be
handed off independently to different attorneys).

At startup, the app resolves the configured vault path and **refuses to run**
(with a clear error) if that path is inside a directory that is itself a git
working tree — a guardrail against accidentally pointing the vault at the repo.
