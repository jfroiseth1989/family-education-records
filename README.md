# FERPA Evidence Manager

A private, local-first tool for organizing a child's educational records
into a searchable, chronologically ordered, fully-cited evidence set.

**This is a software architecture/record-keeping tool, not legal advice.**
It never asserts legal conclusions; it surfaces gaps and conflicts for the
user (or their attorney) to interpret.

## Status

**Phases 1–4 implemented and tested.** Phase 4.5 (relationship graph) is
next — see `docs/PROJECT_PLAN.md` for the full roadmap.

- **Phase 1 — Foundations.** Case management; document ingestion
  (SHA-256 hashing, read-only storage, exact-duplicate rejection);
  document version tracking (a corrected/reissued record is a new,
  linked row, never an overwrite); the per-document chain-of-custody
  ledger.
- **Phase 2 — Extraction, Search & Annotations.** Per-format text
  extraction (PDF, DOCX, plain text/RTF, email); page-level storage with
  offsets; a `needs_ocr` heuristic for low-text-yield pages; full-text
  search (filterable by case/date/tag/record type/needs-OCR); case-scoped
  tags; a document viewer with highlight/note/bookmark annotations and
  its own notes-search index.
- **Phase 3 — OCR.** Tesseract-based OCR (fully offline) with a
  background job queue; an OCR review UI (confidence display, side-by-side
  original vs. recognized text, manual corrections that never overwrite
  the raw OCR output, on-demand reprocessing); OCR provenance surfaced in
  search results.
- **Phase 3.5 — Fact & Observation Layer.** `verified_facts` and
  `ai_observations` are physically separate tables — a machine-suggested
  candidate (currently: a deterministic regex date-parser only, no
  model) is never usable by anything downstream until a human reviews
  and promotes it. A human can also assert a verified fact directly from
  an existing citation. Every fact carries a required citation and a
  confidence level.
- **Phase 4 — Timeline.** Timeline events are built exclusively from
  `verified_facts` — never from raw extracted text or an unreviewed
  observation. Each event has exactly one date-source fact (its date is
  copied from that fact and never changes afterward) plus optional
  supporting facts, addable incrementally over time. The timeline view
  is filterable by event type and date range, shows the day-gap between
  consecutive events, and every event traces in one click back to its
  source citation(s). Soft-delete only — nothing is ever hard-deleted.

Standing constraints across every phase: no AI/LLM or cloud service of
any kind is used anywhere in the current implementation (the OCR engine
is the offline Tesseract binary; the only "AI observation" source is a
deterministic regex date-parser); nothing leaves the local machine;
original documents are opened read-only and never modified; every
citation is immutable once created; and every state-changing action is
logged to an append-only audit trail (per-document chain of custody, or
the case-level audit log for entities that span documents).

Planning documents:
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture, tech
  stack, folder structure
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — database design
- [`docs/PRIVACY_SECURITY.md`](docs/PRIVACY_SECURITY.md) — privacy/security
  plan and threat model
- [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) — roadmap, open decisions,
  and risks
- `docs/PHASE_1_FREEZE.md`, `docs/PHASE_1_REVIEW.md`,
  `docs/PHASE_2_PLAN.md`, `docs/PHASE_2_FREEZE.md`,
  `docs/PHASE_2_STEP_1_FREEZE.md`, `docs/PHASE_3_PLAN.md`,
  `docs/PHASE_3_DECISIONS.md`, `docs/PHASE_3_IMPLEMENTATION_PLAN.md`,
  `docs/PHASE_4_IMPLEMENTATION_PLAN.md` — per-phase design records:
  scope, schema, decisions, and freeze/review notes for each completed
  phase

## Running it

Requires Python 3.11+. From the repo root:

```bash
./scripts/start.sh        # macOS/Linux
scripts\start.bat         # Windows
```

This creates a `.venv/`, installs dependencies from PyPI only, runs
database migrations, and starts the app at `http://127.0.0.1:8420`
(loopback-only — never exposed on your network), opening it in your
browser. Re-running is safe; setup steps are skipped if already done.

By default, case data lives in `~/FERPA-Evidence-Vault/`. To use a
different location, set `FERPA_VAULT_PATH` (e.g. in a `.env` file in the
repo root, or as an environment variable) before starting — the app
refuses to start if that path is inside a git repository, including this
one. To set up a non-default vault ahead of time:

```bash
.venv/bin/python scripts/init_vault.py --path /path/to/your/vault
```

### Optional: OCR support

Phase 3's OCR features need the Tesseract binary installed separately —
`pytesseract` (already a dependency) is just a Python wrapper around it.
Without it, everything else works normally; OCR jobs simply fail with a
clear "Tesseract binary not found" error instead of running.

```bash
brew install tesseract          # macOS
sudo apt install tesseract-ocr  # Debian/Ubuntu
```

## Running the tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The suite (459 tests as of Phase 4) covers every phase above end-to-end:
the git-repo vault guardrail, file hashing/read-only enforcement,
ingestion and version linking, the chain-of-custody and audit-log
ledgers, migrations (including a regression guard against a recurring
Alembic autogenerate false-positive on the FTS5 search tables),
extraction and full-text search, tagging, annotations, OCR execution and
correction, the verified-fact/observation review workflow, and timeline
creation/filtering/soft-delete — plus the case/document HTTP routes
end-to-end. Every test runs against a throwaway vault under pytest's
`tmp_path` — nothing touches a real vault or the repository.

## Core privacy guarantee

This repository contains **source code only**. Case data (original records,
extracted text, OCR output and corrections, tags, annotations, verified
facts, timeline events, the database) lives in a separate "vault"
directory outside this repo, by default at `~/FERPA-Evidence-Vault/`, and
is never committed, pushed, or uploaded anywhere by the application. No
AI/LLM API or cloud service of any kind is called by the current
implementation — OCR runs fully offline via the local Tesseract binary,
and the only automated "observation" source is a deterministic,
non-model regex date-parser. See `docs/PRIVACY_SECURITY.md` for the full
plan.
