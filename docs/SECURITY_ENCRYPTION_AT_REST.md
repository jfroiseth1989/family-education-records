# Encryption at Rest — Architecture & Migration-Risk Analysis

**Status: planning only.** Nothing in this document is implemented. No
vault is encrypted, no migration exists, and no code in this repository
depends on anything here. This is the design and risk register to review
and approve *before* any implementation step begins, per the Security
Phase plan (see `docs/PRIVACY_SECURITY.md` §5).

## 1. What problem this solves

Since the Security Phase's authentication work (`docs/PRIVACY_SECURITY.md`
§6), the web UI is gated by a password. That protects against another
user or process *on this machine* casually opening a browser tab to
FERChronos. It does **not** protect the vault's files on disk against
someone who reads them directly — a stolen drive, a second OS account
with filesystem access, a backup copied somewhere less trusted, or
malware that doesn't go through the browser at all. Today the only
defense against that is OS-level full-disk encryption (BitLocker/
FileVault), which the owner is responsible for enabling and the app
cannot verify or control.

Application-level encryption closes that gap: even if someone gets the
raw vault files, without the owner's password or recovery key the
content is unreadable.

## 2. Constraints this design must satisfy

1. **FTS5 compatibility is non-negotiable.** Full-text search
   (`document_text_fts`, `annotation_notes_fts`) is a core, already-shipped
   feature. SQLite's FTS5 virtual tables need to see plaintext to build
   and query their index. **Field-level encryption (encrypting individual
   column values) would break search outright** — the indexed text would
   be ciphertext, unsearchable by any real query a user would type. This
   is why encryption at rest has to be a *whole-database* mechanism,
   operating below SQLite's own table/index layer, not a per-column
   scheme layered on top of the existing schema.
2. **Password and recovery-key rotation must stay cheap.** Both already
   work today (Security Phase Steps 4-5) and must keep working
   identically from the owner's point of view. Whatever encrypts the
   vault must not require re-encrypting potentially gigabytes of case
   data every time the owner changes their password or rotates their
   recovery key.
3. **Nothing existing may be weakened or lost.** Every original file's
   hash-verification story, every citation's immutability, every
   append-only ledger (`document_custody_events`, `audit_log`,
   `ocr_text_history`, `ocr_corrections`), and the FTS5 indexes
   themselves must all survive a migration to an encrypted vault bit-for-
   bit equivalent in meaning, even though their on-disk representation
   changes.
4. **Still fully local, still zero network calls.** Key derivation and
   encryption must use local, well-reviewed cryptographic libraries only
   — no cloud KMS, no external key escrow, consistent with every other
   privacy commitment in this project.
5. **A lost password and a lost recovery key must mean permanent
   inaccessibility, honestly.** This is already the stated policy for the
   web UI (`docs/PRIVACY_SECURITY.md` §6); once this ships, it becomes
   true of the data itself, not just the login screen. There is no
   backdoor, master key, or third-party recovery path in this design, by
   requirement.

## 3. Chosen approach: whole-database encryption via SQLCipher

**SQLCipher** is a widely-used, open-source SQLite extension that
transparently encrypts the entire database file page-by-page with
AES-256, below SQLite's own storage engine. Because encryption happens
beneath the page cache rather than at the column level, everything built
on top of it — including FTS5 virtual tables — continues to work exactly
as it does today; SQLite doesn't know or care that the pages it's reading
are being decrypted on the way in. This directly satisfies constraint #1
and is the deciding reason to choose it over a field-level scheme (e.g.
`Fernet`-encrypting individual `TEXT` columns via SQLAlchemy type
decorators), which was considered and rejected specifically because it
would silently break search.

### 3.1 Envelope encryption (for cheap key rotation)

Rather than deriving SQLCipher's page-encryption key directly from the
owner's password, use envelope encryption:

- A random **Data Encryption Key (DEK)** — generated once, never
  derived from anything guessable — is what SQLCipher actually uses to
  encrypt the database (`PRAGMA key`).
- The DEK itself is encrypted ("wrapped") by a separate **Key Encryption
  Key (KEK)**, derived from the owner's password via Argon2id (the same
  library already used for password hashing/verification —
  `app/core/auth/passwords.py` — used here in its raw-output KDF mode,
  not its hash-and-verify mode).
- A **second, independent wrapping** of the same DEK is derived from the
  recovery key via the same Argon2id KDF. Either secret — password or
  recovery key — independently unwraps the same DEK.
- Both wrapped-DEK blobs (ciphertext, plus the salt/parameters Argon2id
  needs to re-derive the KEK) are safe to store in plaintext: they are
  opaque and useless without the corresponding password or recovery key.

This is what satisfies constraint #2: changing the password only
re-wraps a ~32-byte DEK under a newly-derived KEK — a sub-second
operation — never touches the multi-gigabyte encrypted database file.
Recovery-key rotation (already implemented, Security Phase Step 5) works
the same way: generate a new recovery key, re-wrap the same DEK under
its KEK, discard the old wrapped blob. **Password reset via recovery key**
(already implemented) becomes: unwrap the DEK using the recovery key's
KEK, then re-wrap that same DEK under a KEK derived from the new
password — the DEK itself, and therefore the encrypted data, never
changes.

### 3.2 The bootstrap problem: two databases, not one

This is the single biggest architectural change this feature requires,
and it needs explicit sign-off before implementation.

Today, `app/main.py::create_app()` opens the database, runs Alembic
migrations, and seeds lookup tables **before serving any request** —
including `GET /auth/setup`, which itself queries `AppAuth` to decide
whether to show the setup form or redirect to login (`app/api/auth.py`).
The whole app currently assumes the database is readable with no secret
at all.

Under SQLCipher, the encrypted database *cannot* be opened without the
DEK, and the DEK cannot be unwrapped without the owner's password or
recovery key — which means the app cannot know whether this is first-run
setup or a returning owner, cannot show the setup/login page correctly,
and cannot even run migrations, until a secret is provided. But the
owner has to submit that secret through a route that itself needs to
render before any secret exists.

**Resolution: split the single `db.sqlite` into two files.**

- **A small, unencrypted "control" database** (e.g. `control.sqlite`)
  holding only `app_auth` and `app_sessions` — exactly the two tables
  that already exist for this purpose, unchanged in shape, plus the two
  new wrapped-DEK columns described in §3.3. This is safe to leave
  unencrypted: it contains no student data, no case data, and no document
  content — only password/recovery-key hashes (already Argon2id, already
  safe to store, same as today) and opaque wrapped-key blobs (useless
  without the corresponding secret). The app can open this file
  unconditionally at startup exactly as it opens `db.sqlite` today, so
  `GET /auth/setup` and `GET /auth/login` keep working with no change to
  their own logic.
- **The existing `db.sqlite`, SQLCipher-encrypted**, holding every other
  table (cases, documents, citations, facts, timeline, FTS5 indexes,
  audit log, custody ledger — everything that is actual case data).
  This connection is only opened **after** a successful login or
  recovery unwraps the DEK; every route that touches case data already
  requires a valid session (deny-by-default middleware, Security Phase
  Step 3), so this lines up naturally with existing enforcement rather
  than fighting it.

Consequences that need to be designed (not yet designed in detail here,
since that's implementation, not architecture):

- Migrations for the control database (Alembic, as today) and the
  encrypted content database become two separate migration chains.
- The content database's migrations, and the startup seeding
  (`seed_document_types`, etc.), can only run once a session exists with
  the DEK available — meaning "run migrations at startup" becomes "run
  migrations at first successful unlock of a session," a real sequencing
  change from today's eager-migrate-before-serving-anything model.
- `app.state.session_factory` (currently one factory for one engine) needs
  to become two factories, one always available (control) and one only
  populated after unlock (content) — likely stored per-session or
  re-established on each authenticated request from a key held only in
  server memory for the session's lifetime, never written to disk. Exact
  mechanism (in-process cache keyed by session id vs. re-deriving per
  request) is an implementation decision for the later coding step, with
  a real performance/complexity trade-off (Argon2id is deliberately slow;
  re-deriving the KEK from the password on every single request would be
  unacceptably slow, so the unwrapped DEK — not the password — needs to
  be what's cached in memory for an active session, cleared on
  logout/lock/expiry).
- Backup guidance (`docs/PRIVACY_SECURITY.md` §7) needs updating once
  this ships: a complete backup requires *both* files.

### 3.3 Schema impact (illustrative — not a migration to run yet)

Two new nullable columns on `app_auth`, populated only once encryption
is actually enabled for a given vault:

- `dek_wrapped_by_password` — the DEK, encrypted under the
  password-derived KEK, plus the Argon2id salt/parameters used.
- `dek_wrapped_by_recovery_key` — the same DEK, encrypted under the
  recovery-key-derived KEK, plus its own salt/parameters.

Both nullable so an existing (unencrypted) vault's `app_auth` row is
already valid schema-wise the moment this ships — encryption becomes
something an owner opts into for an existing vault (§5), not something
forced on upgrade.

## 4. The loose original files are a separate problem

SQLCipher only encrypts the SQLite database file. It has no effect on
the original documents themselves, which are **not** stored as database
blobs — they're loose files on disk under
`<vault>/cases/<case>/originals/` (`app/core/vault.py`), read-only,
hash-verified against `documents.sha256_hash`. Encrypting the database
but leaving these files in plaintext would be a materially incomplete
guarantee: the single most sensitive artifacts in the vault (the actual
IEPs, evaluations, emails) would still be readable directly off disk.

This needs its own design decision, with a real trade-off either way:

- **(a) Also encrypt each original file with the same DEK**
  (authenticated encryption, e.g. AES-256-GCM per file, a random nonce
  per file so two identical files don't produce identical ciphertext).
  Reading a document for display/export means decrypting into memory on
  the fly; SHA-256 verification (`docs/PRIVACY_SECURITY.md` §3) must be
  checked against **the plaintext**, meaning the hash recorded at ingest
  is still the hash of the real, original bytes — verification decrypts
  first, then hashes, exactly like today, just with a decryption step
  inserted before the existing hash comparison. This is the option that
  actually delivers on "the vault's contents themselves become
  permanently unreadable" (the wording already used in the recovery-key
  warnings), and is the recommended direction — but it's genuinely more
  implementation work than the database alone, and needs its own
  performance check for large PDFs/scanned images.
- **(b) Rely on OS-level full-disk encryption for the loose files, and
  scope this feature to "the database only."** Simpler, but weaker and
  arguably misleading if described to the owner as "your vault is
  encrypted" when the actual evidentiary documents on disk are not. Not
  recommended, but flagged because it is meaningfully cheaper.

**Recommendation: (a).** The rest of this document assumes it, but the
choice should be explicitly confirmed before implementation, since it
materially changes scope and timeline.

## 5. Opt-in, not forced, and never automatic

Encryption should be something the owner turns on for their vault, not
something imposed silently on upgrade:

- A brand-new vault can offer to enable encryption during first-run
  setup, in addition to the password/recovery-key flow that already
  exists.
- An **existing, already-populated** vault needs an explicit,
  owner-initiated "Encrypt my vault" action, never a silent migration
  that runs because the app was updated. See §6 for exactly what that
  action must do.
- The app must always know, per vault, whether it's encrypted (a flag in
  the control database, or the mere presence/absence of the
  `dek_wrapped_by_*` columns being populated) and behave correctly either
  way — this is not a one-time flag day across all installs.

## 6. Migration strategy for an existing (unencrypted) vault

This is the highest-risk part of the whole feature, because it runs
against **real case data that already exists**, unlike every other
Security Phase step so far (which only ever added new tables/columns or
new enforcement logic, never rewrote existing data in place).

Required sequence, none of it implemented yet, all of it needing its own
step-by-step approval before running against anyone's real vault:

1. **Preflight checks.** Enough free disk space to hold the encrypted
   copy alongside the original during migration (roughly 2x current vault
   size, momentarily). Vault not currently open elsewhere. No OCR job
   currently running (§ existing worker/queue design).
2. **Back up first, unconditionally.** Copy the entire vault (control +
   content databases, all original files) to a location the owner
   chooses, *before* touching anything. The migration must refuse to
   proceed without a confirmed backup location. This backup is never
   auto-deleted by the app.
3. **Export the plain database into a new encrypted one.** SQLCipher
   provides exactly this primitive (`sqlcipher_export()` — attach an
   empty encrypted database, copy every table/index into it, verified by
   SQLCipher itself as part of the attach/export). This is the standard,
   documented SQLCipher migration path, not something bespoke.
4. **Encrypt the loose original files** (per §4a), writing alongside the
   plaintext originals rather than overwriting them in place, so a
   failure partway through never leaves a file that exists in neither a
   readable plaintext nor a readable encrypted form.
5. **Verify before committing to the new state.** Re-run every original
   file's SHA-256 check against the decrypted form of its new encrypted
   copy; confirm row counts (and, for the append-only ledgers
   specifically, that every row present before migration is still
   present after) match between old and new content databases; confirm
   FTS5 queries against the new encrypted database return the same
   results as against the old plaintext one for a sample of existing
   search terms.
6. **Only after verification passes**, atomically swap the vault to
   point at the new encrypted files, and leave the pre-migration backup
   from step 2 in place — never delete it automatically. The owner
   decides when they're comfortable removing it themselves.
7. **Rollback path**, for if step 5 fails or the owner wants to undo
   this later: SQLCipher's export mechanism works in reverse
   (encrypted → plain) exactly as it does forward; combined with the
   untouched backup from step 2, decrypting back to a plain vault is
   always possible without any code path that could strand the owner
   with only encrypted data and no way back if they ever needed to
   downgrade or if a packaging problem (§7) made the encrypted path
   unusable on their machine.

## 7. Packaging risk — the biggest open unknown

SQLCipher is not part of Python's standard `sqlite3` module. Using it
from SQLAlchemy requires either:

- A third-party PyPI package providing a `sqlite3`-compatible DBAPI
  backed by libsqlcipher (e.g. `sqlcipher3` /
  `sqlcipher3-wheels`/`pysqlcipher3`-family packages — naming and
  maintenance status of these shifts over time and needs a fresh check
  at implementation time, not assumed from this document), configured as
  a custom SQLAlchemy dialect/creator function; or
- Compiling SQLCipher from source against OpenSSL and linking it
  in — viable but adds a real build-toolchain dependency this project
  has deliberately avoided so far (compare: Tesseract for OCR is an
  optional feature that degrades gracefully without it; the database
  layer is not optional — if SQLCipher isn't available, nothing works).

This directly threatens the project's current "installs from PyPI only,
`pip install -e .` and go" simplicity (`README.md`, `scripts/start.sh`/
`start.bat`) if a chosen package doesn't ship prebuilt wheels for all
three target platforms (Windows, macOS, Linux) and both common CPU
architectures (x86_64 and arm64, given Apple Silicon).

**Required before any implementation step is approved: a packaging
spike**, scoped narrowly and separately from the rest of this feature:

1. Pick a candidate SQLCipher Python package.
2. Confirm it installs cleanly via `pip install` alone (no system
   package manager step, no manual OpenSSL/build-tools install) on a
   clean Windows machine, a clean macOS machine (both Intel and Apple
   Silicon if feasible), and a clean Linux machine.
3. Confirm a SQLAlchemy engine can be created against it, that
   `PRAGMA key`/rekey operations work, and — critically — that FTS5
   virtual tables work normally inside an encrypted database (expected
   to work, since FTS5 operates above SQLCipher's page-encryption layer,
   but must be verified directly rather than assumed).
4. Only once that spike succeeds cleanly on all target platforms does it
   make sense to schedule the rest of this feature as an implementation
   step. If it doesn't succeed cleanly, this document's whole approach
   needs to be revisited (e.g. bundling a precompiled SQLCipher binary
   per platform, which is a materially bigger packaging/maintenance
   commitment than anything else in this project so far).

## 8. Performance impact (to be measured, not assumed)

- Argon2id key derivation is deliberately slow (that's the point, for
  resisting offline brute-force of the password/recovery key) — this
  already happens once per login today for password *verification*; this
  feature adds a second, similarly-costed derivation for the KEK, once
  per login/unlock, not per request. Should stay well under a second on
  reasonable hardware, but needs measuring, not assuming.
- SQLCipher's page-level AES-256 encryption adds CPU overhead to every
  database read/write — typically small (SQLCipher's own benchmarks
  suggest single-digit-percent overhead for typical workloads), but this
  project's specific workload (FTS5 queries across potentially years of
  OCR'd document text) should be spike-tested against a realistically
  sized vault before committing to a timeline, not assumed acceptable
  from general benchmarks alone.
- File-level encryption/decryption (§4a) adds latency to viewing/
  downloading a document and to export/binder generation — needs
  measuring against realistically large PDFs and scanned-image files,
  not just small test fixtures.

## 9. What "done" looks like (for a future, separately-approved
   implementation step)

Not proposed for approval now — recorded here so the eventual
implementation step has a clear target to plan against:

1. Packaging spike (§7) passes on all target platforms.
2. Envelope-encryption key management module: DEK generation, Argon2id
   KEK derivation (password and recovery-key variants), wrap/unwrap,
   with its own full test suite independent of the rest of the app.
3. Control/content database split (§3.2): schema, migration sequencing,
   session-lifetime in-memory DEK handling, updated startup flow.
4. Migration tool (§6): backup, export, verify, atomic swap, documented
   rollback — exercised against a realistic synthetic vault (many cases,
   documents, OCR text, FTS5-indexed content, append-only ledger rows)
   before ever running against anyone's real data.
5. Original-file encryption (§4a) wired into ingestion, viewing,
   download, and export/binder generation, with hash verification
   updated to decrypt-then-hash.
6. Performance results (§8) reviewed and found acceptable before this
   ships as anything other than an experimental opt-in.
7. `docs/PRIVACY_SECURITY.md` §5 updated from "not yet implemented" to a
   description of the shipped design, and README's "Not yet implemented"
   callout removed.

Until all of the above is complete, tested, and separately approved,
**no vault is encrypted, and this document changes nothing about how
FERChronos runs today.**
