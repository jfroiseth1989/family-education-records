# FERPA Evidence Manager

A private, local-first tool for organizing a child's educational records
into a searchable, chronologically ordered, fully-cited evidence set.

**This is a software architecture/record-keeping tool, not legal advice.**
It never asserts legal conclusions; it surfaces gaps and conflicts for the
user (or their attorney) to interpret.

## Status

**Phase 1 — Foundations: implemented, awaiting review.** Case management,
document ingestion (hashing, read-only storage, dedup), document version
tracking, and the per-document chain-of-custody ledger are built and
tested. Phase 2 (extraction, search, annotations) has not started — see
`docs/PROJECT_PLAN.md`.

Planning documents:
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture, tech
  stack, folder structure
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — database design
- [`docs/PRIVACY_SECURITY.md`](docs/PRIVACY_SECURITY.md) — privacy/security
  plan and threat model
- [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) — roadmap, open decisions,
  and risks

## Running it

Requires Python 3.11+. From the repo root:

```bash
./scripts/start.sh        # macOS/Linux
scripts\start.bat         # Windows
```

This creates a `.venv/`, installs dependencies from PyPI only, and starts
the app at `http://127.0.0.1:8420` (loopback-only — never exposed on your
network), opening it in your browser. Re-running is safe; setup steps are
skipped if already done.

By default, case data lives in `~/FERPA-Evidence-Vault/`. To use a
different location, set `FERPA_VAULT_PATH` (e.g. in a `.env` file in the
repo root, or as an environment variable) before starting — the app
refuses to start if that path is inside a git repository, including this
one. To set up a non-default vault ahead of time:

```bash
.venv/bin/python scripts/init_vault.py --path /path/to/your/vault
```

## Running the tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The suite (49 tests as of Phase 1) covers the git-repo vault guardrail,
file hashing/read-only enforcement, ingestion (including exact-duplicate
rejection), the chain-of-custody ledger (including tamper detection),
version linking (including the database-level "exactly one current
version per group" constraint), migrations, and the case/document HTTP
routes end-to-end. Every test runs against a throwaway vault under
pytest's `tmp_path` — nothing touches a real vault or the repository.

## Core privacy guarantee

This repository contains **source code only**. Case data (original records,
extracted text, OCR output, the database, generated binders) lives in a
separate "vault" directory outside this repo, by default at
`~/FERPA-Evidence-Vault/`, and is never committed, pushed, or uploaded
anywhere by the application. See `docs/PRIVACY_SECURITY.md` for the full
plan.
