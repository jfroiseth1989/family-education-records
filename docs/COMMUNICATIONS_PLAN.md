# Communications / Email Import Phase — Implementation Plan

Status: **Approved for implementation.** Investigation and full plan were
presented and approved with two decisions resolved before any code was
written (see §1). This document is the durable record of that plan —
the chat-delivered investigation covered the same ground in more detail
(Yahoo's current authentication landscape, credential-storage research,
schema rationale, testing strategy); this file is what future steps and
future readers of this repo should treat as authoritative.

## 1. Decisions locked in

1. **Yahoo authentication: app password over read-only IMAP, now — not
   OAuth2.** Yahoo discontinued real-password IMAP auth in 2024; the
   consumer-facing replacement is a per-app password generated from
   Yahoo's own Account Security page, never FERChronos's real Yahoo
   password. Full OAuth2 (SASL XOAUTH2/OAUTHBEARER) is documented but
   gated behind Yahoo's discretionary, non-self-serve approval process
   for the `mail-r` scope — not something this project can turn on by
   writing code. The auth layer is structured so an `"oauth2"` value can
   be added to `CommunicationAccount.auth_method` later *if* Yahoo
   approves access, without a schema change — but no OAuth plumbing is
   built speculatively, and nothing about this phase is blocked on it.
2. **Attachment classifier: "Reevaluation"/"Eligibility Determination"
   are trigger synonyms for the existing "Evaluation" `DocumentType`,
   not new types.** No new `DocumentType` rows are added for them at this
   stage. Suggestion coverage is also extended to the original Phase 1
   document types (Evaluation, Correspondence, 504 Plan, Discipline,
   Attendance, Grades, Medical, Legal Filing, Audio Transcript), which
   currently have no trigger phrases in `app/core/document_type_suggestion.py`
   at all (only the 17 types added in FERChronos Step 5.6 do). This is a
   Step 5 concern (attachment classification); recorded here now so the
   decision isn't re-litigated later.
3. **Manual `.eml` upload is fully independent of Yahoo.** The shared
   Communications pipeline (parsing, threading, attachment detection,
   document linking, search, timeline suggestions) must work with zero
   connected mailbox. `Communication.account_id` is nullable specifically
   for this reason (see §2).

Every other requirement from the original request carries forward
unchanged: raw originals preserved unmodified, hashing/custody/
provenance retained, FERChronos-generated material structurally separate
from preserved originals, no silent/background network polling, no
mailbox write operations (IMAP is read-only by construction, not just by
convention), no plaintext external credential anywhere in the SQLite
database, no duplicate `Document` rows when the same attachment arrives
through multiple emails (multiple provenance links to one document
instead), and full backward compatibility with everything that already
exists.

## 2. Schema (Step 1)

All tables below are purely additive — no existing table's columns
change. See `app/db/models.py` for full docstrings on each; summarized
here for reference:

- `communication_accounts` — one row per connected mailbox.
  `credential_ref` is an opaque OS-keyring lookup key; the actual
  credential is never stored in this database. Not a singleton.
- `communications` — one row per imported message (the email-world
  analog of `documents`): hashed, read-only-stored raw `.eml` bytes,
  parsed headers/body, nullable `account_id` (manual upload) and
  `case_id` (assigned later). Deduplicated primarily on
  `(account_id, message_id_header)`, falling back to
  `(account_id, sha256_hash)` in application logic when no Message-ID
  is present.
- `communication_threads` — reconstructed conversations, built from
  Message-ID/In-Reply-To/References (subject/participants/date as a
  fallback only); never merges the underlying `communications` rows.
- `communication_attachments` — one row per attachment, hashed before
  any classification/promotion decision. `is_educational_record_candidate`/
  `suggested_document_type_id` are advisory only, from a local
  deterministic classifier (Step 5), never applied automatically.
- `communication_document_links` — many-to-many provenance between a
  `Document` and every communication/attachment that produced or
  re-delivered it. The same IEP received via three emails yields three
  link rows pointing at one `Document`, never three documents.
- `communication_custody_events` — append-only ledger mirroring
  `document_custody_events` exactly, scoped to communications.
- `communication_import_batches` / `communication_import_batch_items` —
  mutable job-lifecycle tables (same justification as `OcrJob`: process
  state, not evidentiary content) backing resumable/cancellable/
  retry-safe bulk import (Steps 9-10).

Vault layout gains one additive path,
`VaultLayout.communications_dir(case_id, label)`, alongside the existing
`originals_dir`, for raw `.eml` files and preserved attachment bytes —
same read-only-on-write convention.

## 3. Step sequence

**A — Foundations**
1. ✅ Communications schema migration + models + vault layout addition.
   No behavior, no UI.
2. ✅ Secure credential module (`app/core/communications/credentials.py`,
   an OS-keyring-only wrapper via the `keyring` package -- no fallback
   storage, fails closed if no OS secret store is reachable) +
   `app/core/communications/accounts.py` (connect/disconnect lifecycle)
   + Connect/Disconnect Yahoo UI (`/communications`). Connecting only
   ever saves the credential and account row -- no IMAP call, no sync,
   no test login. Disconnecting deletes the credential and marks the
   account disconnected without touching any imported data (there is
   none yet to touch). *(this step)*

**B — Manual upload & the shared content pipeline**
3. Manual `.eml` upload → `communications`/`communication_attachments` +
   custody events, extending `app/core/extraction/email.py`. Basic
   Communications nav + list views.
4. Thread reconstruction + thread view.
5. Attachment educational-record detection (extended trigger coverage
   per §1 decision 2) + single-item attachment review + "Add to
   Documents" via the existing auto-fill preview + document↔email
   linking + cross-email duplicate-document detection.
6. `communication_text_fts` (new, independent FTS5 table) + Communications
   search/filter UI.
7. Timeline suggestions from communications (approve/edit/reject),
   linked back to source email.
8. Investigate and, if reliable, add `.msg`/other saved-email formats,
   each with its own parser rather than forcing an unreliable fit.

**C — Automatic Yahoo import**
9. Read-only IMAP connectivity (app-password auth) + folder listing +
   search-by-criteria, returning counts/previews only.
10. Bulk import engine (batched/resumable/cancellable/retry-safe) + the
    pre-import review screen + batch case assignment.
11. Bulk attachment-review screen, built on Step 5's logic.
12. Progress/status UI (mirroring the OCR jobs page) + verification that
    Disconnect Yahoo only ever removes credentials, never imported data.
13. *(Gated on Yahoo approving access)* OAuth2 as an added, preferred
    auth option — only if and when approval actually comes through.

Each step: implement → targeted tests → full regression suite → FTS5
guard → scope check → backward-compatibility check → original-file
integrity verification → live smoke test → commit → push → pause for
review.
