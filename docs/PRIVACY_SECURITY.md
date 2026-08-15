# FERPA Evidence Manager (FERChronos) — Privacy & Security Plan

Status: **§1-4 and §7-10 reflect the Phase 1 design and remain current.
§5 (at-rest protection) and §6 (access control) are updated for the
Security Phase, which added local password authentication, CSRF
protection, session management, and password recovery — see each
section below for exactly what's implemented versus still planned.**

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

### 2.1 Outbound network exception: read-only Yahoo IMAP (Communications
Phase Step 9)

FERChronos's first, and so far only, outbound network call: reading and
searching a user's own connected Yahoo mailbox over IMAP, so the
Communications feature can show what mail exists before importing any
of it (Step 9) and, later, actually import it (Step 10). This is a
deliberate, narrow exception to §2 above, not a relaxation of it — every
other guarantee in this document is unchanged, and the exception itself
is bounded as follows:

- **User-initiated only, every time.** FERChronos never opens a
  connection to Yahoo on its own — not at startup, not on a timer, not
  in the background. A connection is opened only when a human clicks
  "Test Mailbox Access" or "Browse/Search Yahoo Mail" (or, from Step 10
  onward, explicitly starts an import). There is no polling, no
  scheduled sync, and no "keep the connection open and watch for new
  mail" behavior anywhere in this application.
- **Host, port, and transport.** `imap.mail.yahoo.com:993`, TLS from the
  moment the socket opens (`IMAP4_SSL` — there is no plaintext IMAP
  fallback anywhere in this codebase).
- **Purpose, narrowly.** Read and search the connected mailbox only.
  `app/core/communications/imap_client.py` is a narrow wrapper exposing
  only connect/authenticate, list folders, read-only mailbox selection,
  search, and a bounded metadata-only preview fetch — there is no method
  on it for STORE, COPY, MOVE, EXPUNGE, DELETE, APPEND, or any other
  flag-mutating IMAP command; every mailbox SELECT it issues passes
  `readonly=True`, and preview/raw-message fetches use `BODY.PEEK` so a
  message is never marked `\Seen` by merely being previewed. FERChronos
  cannot modify, delete, move, or mark anything in a connected Yahoo
  mailbox — not through a bug elsewhere in the app, because the
  capability to do so does not exist in the code at all.
- **No telemetry, no cloud AI, anywhere in this path.** The IMAP
  connection carries only the IMAP protocol itself — no analytics, no
  usage reporting, no request ever leaves this device except the direct
  socket to Yahoo's own mail server the user explicitly asked to reach.
  Nothing fetched from Yahoo is ever sent to any AI/LLM service, cloud
  or local.
- **Credentials never leave OS secure storage.** The Yahoo app password
  is retrieved from the OS-native credential store
  (`app/core/communications/credentials.py`) only for the duration of
  one connection, handed directly to the IMAP `LOGIN` command, and never
  written to SQLite, a log file, an exception message rendered to the
  user, a URL, or a query parameter. See §3's original-record-integrity
  guarantees and `docs/COMMUNICATIONS_PLAN.md` §3/§16 for the credential
  storage design this reuses unchanged.
- **Step 9 imports nothing.** As of Step 9, browsing and searching a
  mailbox is inspection only — no `Communication`,
  `CommunicationAttachment`, vault file, or custody event is ever
  written by these routes. The UI makes this explicit ("No email has
  been imported yet") so a search result is never mistaken for evidence
  already stored in FERChronos.
- **Connecting a Yahoo account is entirely optional.** Every Communications
  workflow that doesn't require live mailbox access — manual `.eml`
  upload, `.mbox` archive import, browsing/searching/reviewing anything
  already imported, Communications search, threads, attachment review,
  Document promotion, timeline suggestions — works identically whether
  zero or several Yahoo accounts are connected, and requires zero
  outbound network access either way (Communications Phase Step 12).
  Manual `.eml`/`.mbox` import in particular never reads
  `CommunicationAccount` at all.

### 2.2 Yahoo account lifecycle: disconnect, reconnect, and credential
failure (Communications Phase Step 12)

- **Disconnecting a mailbox never deletes evidence.** "Disconnect" means
  exactly two things: delete the stored app password from OS secure
  storage, and mark the account `disconnected` in the database. Every
  `Communication`, `CommunicationAttachment`, import batch/batch item,
  `Communication`↔`Document` provenance link, `Document`, custody event,
  timeline suggestion, and full-text search entry already produced
  through that account is untouched and stays exactly as usable —
  including offline, with the Yahoo account never reconnected again.
  Only further *Yahoo-dependent* work (testing the connection, browsing/
  searching the live mailbox, starting or resuming an import) becomes
  unavailable until the account is reconnected.
- **An unfinished import batch is never silently retried or lost.** If a
  batch still has `pending`/`failed` items when its account is
  disconnected, those items stay exactly as they are — not reprocessed,
  not deleted, not marked invalid. Resuming or retrying such a batch is
  a safe no-op while the account has no valid connected credential (no
  network attempt is made), and the batch's status page explains that
  reconnecting the account is required before that work can continue.
- **Reconnecting the same Yahoo address reactivates the existing
  account** rather than creating a second logical account record —
  every `Communication` already imported through it, and the
  account-scoped duplicate-import detection Step 10 relies on, both
  stay correctly associated with the one account identity. Reconnecting
  never re-imports anything automatically and never resumes a batch on
  its own; both require an explicit, separate user action, same as a
  first-time import.
- **A missing or revoked credential fails closed, safely.** If the OS
  secure credential store no longer has an entry for a `connected`
  account (deleted outside FERChronos, or the underlying app password
  was revoked in Yahoo), every route that would need it — test
  connection, browse, import — returns a plain, safe error message
  ("reconnect this account") and changes nothing; the account's
  `credential_ref` (an opaque lookup key, never the secret itself) is
  never echoed back, and no already-imported `Communication`,
  `CommunicationAttachment`, or `Document` is ever touched, marked
  invalid, or deleted because of an authentication failure. This is
  always treated as a connection problem, never as evidence corruption.
- **No language anywhere implies a continuous, live connection.** The
  Communications UI's account status distinguishes "Connected" (a valid
  credential is present) from "Credential unavailable" (the database
  says connected, but the OS credential store currently has nothing for
  it) from "Disconnected," and reiterates on every disconnected/
  credential-unavailable account row that its already-imported evidence
  remains local and fully usable. There is no background sync of any
  kind to describe as "live" in the first place — see §2.1 above.

### 2.3 Saved-email formats accepted, and why (Communications Phase Step 8)

- `.eml` (single message, manual upload) and `.mbox`/`.mbx` (an archive
  split into its individual original messages, each imported the same
  way as a single `.eml`) are the only saved-email formats FERChronos
  parses today. Both work with zero Yahoo account involvement.
- `.msg` (Outlook/MAPI) was evaluated and deliberately left unsupported
  for now — see `docs/COMMUNICATIONS_PLAN.md` Step 8 for the full
  investigation. It is not silently converted or guessed at.
- A PDF or screenshot *of* an email is never treated as a `Communication`
  — it has no parseable original headers/threading data to preserve —
  and is instead ingested as a normal `Document`, exactly like any other
  scanned or exported file.

## 3. Original record integrity (read-only originals)

- Ingestion always **copies** the source file into `originals/`; the app
  never opens an original in write mode.
- The copied file is set read-only at the OS level where supported
  (Windows/macOS both support this via file attributes).
- SHA-256 is computed at ingest and stored; it is re-verified before every
  export or binder generation, so silent corruption or tampering is
  detectable, not just theoretically prevented.
- OCR corrections are stored as a separate annotation layer, never as an
  overwrite of the original OCR output or the source file.
- **Highlights, bookmarks, and notes never touch the source file.** They are
  database rows (`annotations`) referencing a document/page/citation and are
  rendered as an overlay in the viewer at display time. No code path opens
  an original file to write an annotation into it; deleting every
  annotation a user ever made would leave the original byte-for-byte
  identical to the day it was imported.
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
- Every relationship in the graph connecting people, organizations,
  documents, meetings, evaluations, IEPs, incidents, transportation
  decisions, providers, or timeline events must cite at least one source
  document (`verified_relationship_citations`) — there is no path to an
  uncited edge, verified or otherwise.

## 5. At-rest protection

**Current status: application-level login gates the web UI (§6), but the
vault on disk is not yet encrypted.** Two layers are relevant and not
mutually exclusive:

- **OS-level full-disk encryption** (BitLocker on Windows, FileVault on
  macOS) — the current baseline the user is responsible for enabling; the
  app can check and warn if it's off, but can't enable it itself. This is
  the only protection today against someone with direct filesystem access
  to the vault (a stolen drive, a second OS account with file access,
  etc.) while the computer is off or the disk is otherwise locked.
- **Application-level encryption** (whole-database encryption via
  SQLCipher, envelope-encrypted so a password/recovery-key change only
  re-wraps a small key rather than the whole file) — architecture and
  migration-risk analysis produced in
  `docs/SECURITY_ENCRYPTION_AT_REST.md`. That document is **planning
  only**: no vault is encrypted yet, and no migration runs until a
  separate, explicitly approved implementation step. The biggest reason
  this can't be silently "just turned on" is FTS5: SQLite's full-text
  search needs plaintext to index, so field-level encryption would break
  search — only whole-database encryption preserves it, which is why this
  needed its own design pass rather than being folded into the
  authentication work in §6.

Until application-level encryption ships, the password/session/CSRF/
lockout controls in §6 protect the **web UI** — they gate what a browser
on this machine can see and do. They do not, by themselves, protect the
vault files on disk from someone who can read them directly (bypassing
the app entirely) if the disk itself isn't encrypted. Both layers matter;
neither substitutes for the other.

## 6. Access control (authentication)

FERChronos is single-owner software — there is no username, no multi-user
login, and no account-existence disclosure anywhere in the UI (wrong
password and "no account yet" are indistinguishable both in wording and
in response timing). The web UI is gated by a local password the owner
sets on first run; everything below is implemented, not planned.

**Authentication.**
- First-run setup asks for a password (minimum 8 characters), hashed with
  Argon2id (`app/core/auth/passwords.py`) — the plaintext password is
  never stored, logged, or transmitted anywhere.
- Login/lock/logout are ordinary POST routes (`/auth/login`, `/auth/lock`,
  `/auth/logout`) protected the same way every other state-changing route
  is (CSRF, below). "Lock" and "logout" are functionally identical (both
  fully end the session); "lock" only changes the login page's copy.

**Deny-by-default route protection.**
- Every route requires a valid session by default
  (`app.core.auth.enforcement.AuthEnforcementMiddleware`). Only `/auth/*`
  (the setup/login/lock/logout/recovery pages themselves) and `/static/*`
  (CSS/JS, no case data) are public — there is no per-route opt-in list to
  keep in sync as routes are added; a new route is protected automatically
  simply by existing outside those two prefixes.
- This includes direct document-file and page-image URLs
  (`/documents/{id}/file`, `/documents/{id}/pages/{n}/image`) — a raw,
  unauthenticated link to a document never bypasses login.
- Every response this middleware handles carries `Cache-Control:
  no-store, private` (except `/static/*`, which is safe to cache), so
  document bytes, page images, and any page with student data are never
  retained by a browser or intermediary cache.

**CSRF protection.**
- A double-submit-cookie token protects every state-changing request
  (POST/PUT/PATCH/DELETE) application-wide, validated in constant time.
  The `/auth/*` forms validate it themselves; every other route is
  covered centrally by the same enforcement middleware, so no route can
  forget to check it.
- Non-browser/API clients: fetch any page once to receive the
  `csrf_token` cookie (set no later than `/auth/login` or `/auth/setup`,
  both of which necessarily precede any authenticated request), then send
  that value back on mutating requests as the `X-CSRF-Token` header —
  no need to multipart-encode a form field alongside a file upload.

**Sessions.**
- Server-side sessions (`app_sessions`) — the browser cookie carries only
  an opaque random token, never session data itself, and a session id is
  only ever minted at successful login (never reused/upgraded from a
  pre-auth cookie), which rules out session fixation by construction.
- **15-minute inactivity auto-lock**, sliding forward on every valid
  authenticated request — not merely on login.
- **12-hour absolute session expiry**, independent of activity.
- An expired or inactive session's row is hard-deleted the moment it's
  detected (not just rejected in place), and the browser's session cookie
  is cleared — a full password login is required afterward either way.
  Session rows are the one deliberate exception to this app's otherwise
  append-only/never-deleted data model (see the `AppSession` docstring in
  `app/db/models.py`): they're ephemeral security state, not an
  educational record.

**Failed-login throttling.**
- After 5 consecutive failed attempts, login locks out for 60 seconds;
  each further failure while still locked doubles the wait, capped at 15
  minutes. An active lockout rejects every attempt immediately — including
  a correct password — so a sustained attacker can't extend the lock
  indefinitely by continuing to hammer it. The lockout message is fixed
  and generic (no attempt count, no unlock timestamp). A successful login
  resets the counter.

**Password recovery.**
- A cryptographically random recovery key (~118 bits of entropy, 6
  groups of 4 characters from an alphabet excluding easily-confused
  characters) is generated at first-run setup and shown to the owner
  exactly once, with a required "I have saved this" confirmation before
  continuing. Only its Argon2id hash is ever stored.
- `/auth/recover` resets the password given a valid recovery key. On
  success: the key is rotated (single-use — the one just used never
  works again), every existing session anywhere is invalidated, the
  failed-login counter resets, and the owner is signed into a fresh
  session showing their new key.
- An authenticated owner can also rotate the key deliberately at any time
  (`/account/recovery-key`), independent of the password.
- **There are no security questions, password hints, hidden master
  password, or cloud/support-desk recovery of any kind.** Losing both the
  password and the recovery key means there is no way back into the
  application today, and once at-rest encryption (§5) ships, it will mean
  the vault's contents themselves become permanently unreadable — not
  just the web UI locked. This is stated explicitly to the owner
  everywhere a recovery key is shown.

**What this does and doesn't cover.** This is a single-local-user access
gate for the web UI, not a multi-tenant or network-facing auth system —
the loopback-only binding in §2 is what actually keeps the app
unreachable from other machines; login/CSRF/sessions/lockout protect
against another process or user *on this machine* casually opening a
browser tab to it. See §5 for what still relies on OS-level disk
encryption until application-level encryption ships, and §8 for the full
threat model.

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
- Casual snooping by another user or process *on this machine* that
  doesn't know the password — see §6. Locking or logging out (or letting
  the 15-minute inactivity timeout do it automatically) closes the web
  UI's access even while the computer itself stays unlocked.
- Repeated password/recovery-key guessing against the web UI (§6's
  escalating lockout) — not a defense against brute-forcing the ~118-bit
  recovery key itself, which is computationally infeasible regardless.
- Undetected corruption or tampering of original records (hash verification).
- Loss of evidentiary traceability (every claim cites an exact source).
- Silent conflation of OCR guesses with verified document text.

**Explicitly out of scope for this design:**
- Someone with full access to your unlocked, already-decrypted computer
  *and* an already-unlocked FERChronos session (e.g. an open browser tab
  within the 15-minute inactivity window) — the OS-level access itself is
  outside this app's control, and defeating an unlocked session on the
  same machine isn't something an in-browser login screen can prevent.
- Someone with direct filesystem access to the vault, bypassing the app
  and its login entirely, on a machine without full-disk encryption
  enabled (§5) — until at-rest encryption ships, the login/session
  controls in §6 protect the web UI, not the files on disk.
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

## 10. AI inference boundary (cross-cutting rule)

One rule, applied identically everywhere the app produces something beyond
a direct citation:

> Nothing the app labels as established was asserted by the app itself. It
> was either directly cited from a source document by a human (a verified
> fact, a verified relationship), or it is explicitly marked as an
> unreviewed machine output pending human judgment (an AI observation, an
> AI-generated summary, a suggested relationship) — and the second category
> can never silently become the first.

This is enforced the same way in all three places it currently applies, and
must be enforced the same way in any future feature that generates a
suggestion:
- **Facts** — `ai_observations` vs `verified_facts` (§3.7 ARCHITECTURE.md).
- **Summaries** — `ai_summaries`, permanently ineligible for promotion to a
  fact under any review status (§3.7 ARCHITECTURE.md).
- **Relationships** — `ai_suggested_relationships` vs `verified_relationships`
  (§3.9 ARCHITECTURE.md).

In each case: the machine-generated row lives in its own table, is never
read by downstream features (timeline, conflicts, binder, graph view) as if
it were confirmed, and promotion — where promotion is even possible —
always creates a new row rather than mutating the suggestion, preserving
the suggestion-to-confirmation lineage.
