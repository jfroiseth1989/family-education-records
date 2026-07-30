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
│  │  ingestion │ extraction │ ocr │ indexing │ timeline │ binder     │  │
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
- Writes an `audit_log` entry for every ingestion.

### 3.2 Extraction
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

### 3.3 OCR
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

### 3.4 Indexing / Search
- SQLite FTS5 virtual table over page-level extracted/OCR text.
- Search UI supports filtering by case, date range, tag, person, record type,
  and needs-OCR status.

### 3.5 Timeline
- Timeline entries are **built from citations, not free text**: every event
  must link to at least one `(document, page, span)` reference. The UI is
  built around "attach this quote to a date" rather than a free-text journal,
  so every timeline claim is traceable to an exact source location.
- Dates can be extracted-and-suggested (regex/date-parsing over text) but are
  always user-confirmed before an event is marked "confirmed" vs "needs review."

### 3.6 Missing / Conflicting Records
- **Missing records**: a user- (or attorney-) defined checklist of expected
  records (`record_requirements`), e.g. "Annual IEP review," with an expected
  date/recurrence. The app flags checklist items with no linked document as
  outstanding. The app does **not** ship a built-in table of legal deadlines —
  that's a deliberate boundary given the "not legal advice" framing (see
  PROJECT_PLAN.md, decision #3).
- **Conflicting records**: a human-driven workflow. The user (or attorney)
  selects two or more spans across documents and flags them as conflicting,
  with a note. The UI supports side-by-side comparison. No automated
  contradiction detection in v1 (flagged as a stretch goal, local-only NLP,
  in PROJECT_PLAN.md).

### 3.7 Evidence Binder
- Assembles selected documents + timeline + citation index into a single
  paginated PDF: cover page, table of contents, exhibit list (numbered,
  matching source documents), chronological timeline with inline citations,
  and appended source documents (merged via PyMuPDF) so every citation in the
  binder can be checked against the actual page it cites.
- Every export is recorded in `binder_exports` (what documents/events were
  included, hash of the resulting file) so a binder can be regenerated or
  audited later — reproducibility matters for evidentiary use.
- Rendered with WeasyPrint (HTML/CSS → PDF) so the binder template reuses the
  same templating system as the web UI.

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
├── db.sqlite                   # all cases, documents, timeline, tags, audit log, FTS5 index
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
