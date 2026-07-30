# FERPA Evidence Manager

A private, local-first tool for organizing a child's educational records
into a searchable, chronologically ordered, fully-cited evidence set.

**This is a software architecture/record-keeping tool, not legal advice.**
It never asserts legal conclusions; it surfaces gaps and conflicts for the
user (or their attorney) to interpret.

## Status

Phase 1 — architecture and project plan only. No application code has been
written yet. See:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture, tech
  stack, folder structure
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — database design
- [`docs/PRIVACY_SECURITY.md`](docs/PRIVACY_SECURITY.md) — privacy/security
  plan and threat model
- [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) — roadmap, open decisions,
  and risks

## Core privacy guarantee

This repository contains **source code only**. Case data (original records,
extracted text, OCR output, the database, generated binders) lives in a
separate "vault" directory outside this repo, by default at
`~/FERPA-Evidence-Vault/`, and is never committed, pushed, or uploaded
anywhere by the application. See `docs/PRIVACY_SECURITY.md` for the full
plan.
