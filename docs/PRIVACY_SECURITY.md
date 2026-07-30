# FERPA Evidence Manager — Privacy & Security Plan (Phase 1)

Status: **Draft for review.**

This document defines the privacy and security posture the architecture is
required to uphold. It is a design constraint, not an afterthought — every
component in ARCHITECTURE.md is expected to comply with this document.

## 1. Repo vs. Vault separation (the core control)

The single most important control in this system: **source code and case
data live in physically different locations, and case data is never inside
a git working tree.**

- The git repository (this one, `family-education-records`) contains only
  application code, docs, and non-identifying config. It is safe to host on
  GitHub.
- The vault (originals, extracted text, OCR output, the database, exports,
  audit log — i.e., everything that could contain a student's PII) defaults
  to a path **outside** the repository, e.g. `~/FERPA-Evidence-Vault/`.
- At startup, the app resolves the configured vault path and refuses to run
  if that path is inside a directory that is itself a git working tree. This
  turns "please don't put case data in the repo" from a README warning into
  an enforced check.
- `.gitignore` additionally excludes `*.sqlite`, `.env`, and any
  `vault/`/`data/`-shaped local paths as defense-in-depth, in case someone
  overrides the default location carelessly.
- Nothing in the app ever runs `git add`/`git commit`/`git push` — there is
  no code path that could programmatically upload case data even by accident.

## 2. No data leaves the device, by default

- The web UI is served only on `127.0.0.1` (loopback). It is never bound to
  `0.0.0.0` or a LAN-visible address in default configuration.
- No telemetry, crash reporting, or analytics SDKs.
- No auto-update mechanism that phones home without an explicit user action.
- OCR runs locally via Tesseract — no cloud vision API (Google Vision, AWS
  Textract, Azure Read) is used, because those would send document images
  containing PII to a third party.
- No LLM/AI API calls in the default build. If local-only NLP assistance is
  added later (Phase 8 stretch goal, e.g. a small model running fully
  on-device for date/entity suggestions), it must preserve the same
  guarantee — no network call, ever, for that feature. If a **cloud** AI
  feature is ever proposed, it must be opt-in per action, disabled by
  default, and show the user exactly what text would be sent before sending
  anything.
- Dependency hygiene: pinned versions, preference for well-maintained
  libraries, periodic `pip-audit`; specifically review PDF/OCR/email-parsing
  dependencies for any built-in network behavior before adopting them.

## 3. Original record integrity (read-only originals)

- Ingestion always **copies** the source file into `originals/`; the app
  never opens an original in write mode.
- The copied file is set read-only at the OS level where supported
  (Windows/macOS both support this via file attributes).
- SHA-256 is computed at ingest and stored; it is re-verified before every
  export or binder generation, so silent corruption or tampering is
  detectable, not just theoretically prevented.
- OCR corrections and user annotations are stored as a separate layer, never
  as an overwrite of the original OCR output or the source file.
- **A corrected or reissued record is never applied as an edit.** A
  corrected IEP, a reissued evaluation, or any updated version arrives as an
  entirely new import — its own file, its own hash, its own database row —
  which the user then links to the earlier version as a relationship
  (`document_version_groups` / `supersedes_document_id`, see
  ARCHITECTURE.md §3.2). Every prior version stays on disk, unmodified,
  indefinitely; "current" is a label on the relationship, never a state that
  replaces or deletes anything.
- **Every imported document has a dedicated, append-only chain-of-custody
  ledger** (`document_custody_events`): the import event captures original
  filename, SHA-256 hash, file size, and storage location, and every
  subsequent action on that document — extraction, OCR, tagging, version
  linking, inclusion in an exported binder — appends a new row rather than
  modifying an existing one. The ledger is not exposed to any
  UPDATE/DELETE path, matching the treatment of `audit_log`.

## 4. Traceability / chain of custody

- Every timeline entry, binder entry, and flagged conflict must reference a
  concrete `(document, page, span)` citation — see DATA_MODEL.md. This is a
  privacy/integrity feature as much as a UX one: it means every claim in a
  generated binder can be checked against the exact page it came from.
- Every document has its own append-only chain-of-custody ledger
  (`document_custody_events`) covering import date, original filename,
  SHA-256 hash, file size, and storage location, plus every subsequent
  action taken on it. `audit_log` remains append-only alongside it for
  case/system-level activity (exports, requirement edits, conflict flags).
  Neither exposes an UPDATE/DELETE path through the app.
- Superseded document versions are never deleted or overwritten — they stay
  in the vault and in the database, linked to the current version via
  `document_version_groups`, so the full version history is always
  reconstructable.
- Every generated binder is recorded in `binder_exports` with a hash of the
  output file, and per-section provenance (`binder_sections` /
  `binder_export_sources`) records exactly which documents/facts contributed
  to each section, so a binder can be reproduced or audited later.

## 5. At-rest protection (open decision — see PROJECT_PLAN.md #2)

Two layers are available and not mutually exclusive:
- **OS-level full-disk encryption** (BitLocker on Windows, FileVault on
  macOS) — assumed as a baseline the user is responsible for enabling; the
  app can check and warn if it's off, but can't enable it itself.
- **Application-level encryption** (SQLCipher in place of plain SQLite, or an
  app passphrase gate) — stronger if the machine is shared or could be lost
  while unlocked, but adds real complexity (key management, "what if you
  forget the passphrase" recovery story). Recommend deciding **before**
  Phase 1 implementation starts, since swapping SQLite → SQLCipher after
  real case data exists is more painful than deciding up front.

## 6. Access control

- v1 assumes a single local OS user account model: whoever is logged into
  the computer can open the app. No built-in multi-user login for a
  single-family local tool.
- If the computer is shared, the mitigations are OS-level user separation or
  the optional app passphrase from §5 — not a custom auth system.

## 7. Backups

- Manual/user-initiated only. The app can offer an explicit "Export vault
  backup" action (copy the vault to a chosen location, e.g. an encrypted
  external drive) as a Phase 7 feature, to reduce user error versus asking
  people to remember to copy a folder correctly.
- No built-in cloud sync. If a user wants offsite backup, that's their
  choice to configure outside the app (e.g., their own encrypted cloud
  drive) — deliberately not built in, to keep the "nothing leaves the
  device by default" guarantee simple and auditable.

## 8. Threat model — what this protects against, and what it doesn't

**In scope / mitigated:**
- Accidental upload of case data to GitHub or any cloud service.
- Casual snooping by anyone without access to the machine/OS account.
- Undetected corruption or tampering of original records (hash verification).
- Loss of evidentiary traceability (every claim cites an exact source).
- Silent conflation of OCR guesses with verified document text.

**Explicitly out of scope for this design:**
- Someone with full access to your unlocked, already-decrypted computer.
- Malware already present on the machine.
- Nation-state-level adversaries.
- Legal correctness of any deadline, rule, or characterization — the app
  never asserts legal conclusions; that judgment stays with the user/attorney.

## 9. Language/UI discipline

Because this tool is explicitly not legal advice: UI copy for gaps and
conflicts uses neutral language ("Possible gap — no record found for
[user-defined requirement]", "Flagged as potentially conflicting by
[user]") rather than conclusory language ("violation," "non-compliant").
This is a content guideline for every phase, not just a one-time review.
