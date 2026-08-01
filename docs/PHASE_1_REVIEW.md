# Phase 1 Completion Review

Status: **Review document — Phase 1 implemented and pushed (commit `0aa54db`
on `claude/ferpa-evidence-architecture-awlfqt`), awaiting owner sign-off
before Phase 2 begins.** No Phase 2 code has been written.

This document is written for you as the system owner, not as legal
guidance — it confirms what was built and why, and flags every place where
a judgment call is still yours to make.

## Checklist of Phase 1 deliverables

### 1. Vault lifecycle & git-repo guardrail
**Implemented.** `app/core/vault.py` resolves the vault path, creates its
directory structure, and — before doing anything else — checks every
ancestor directory for a `.git` entry (directory or worktree file) and
refuses to proceed if found (`VaultInsideGitRepoError`).

**Why it matters.** This is the one control the entire "no case data on
GitHub" promise rests on. If it fails silently, everything downstream
(read-only originals, custody logs, hashes) is moot the moment someone
runs `git add -A` in the wrong place.

**Verified, not just asserted.** Tested against a synthetic `.git` marker
*and* a real `git init` repo (`tests/test_vault.py`), and manually
confirmed at startup — the running instance's vault (`~/FERPA-Evidence-Vault`
by default) is outside this repository.

**Risks / decisions for you.** None outstanding for this control itself.
One adjacent decision from `docs/PROJECT_PLAN.md` #1 is still open: is
`~/FERPA-Evidence-Vault` the right default location for your machine, or
do you want a different default?

### 2. Database schema via Alembic migrations (not `create_all`)
**Implemented.** `app/db/models.py` defines the schema; `app/db/migrate.py`
runs `alembic upgrade head` at every startup — there is no code path that
creates tables any other way, including in tests.

**Why it matters.** Every future phase adds tables/columns via a reviewable
migration, never a silent schema reconciliation. If Phase 2 needs to change
something, the migration file itself is the record of what changed and when.

**Risks / decisions for you.** None for Phase 1. Worth knowing: Alembic's
autogenerate is a starting point, not a guarantee (noted in
`app/db/migrations/README`) — future migrations should still be
hand-reviewed, which is standard practice, not a gap specific to this app.

### 3. Case management (CRUD)
**Implemented.** Create, list, view, and edit a case (label, description,
status). Every create/edit writes an `AuditLog` entry.

**Why it matters.** A case is the container everything else hangs off of;
its audit trail establishes when the case record itself was opened or
relabeled, which matters if the case's own metadata is ever questioned.

**Risks / decisions for you.** Deliberately **no delete** endpoint for
cases in Phase 1 — cascade-deleting a case would cascade-delete its
documents, which felt like the wrong default for an evidence tool. If you
ever need to remove a case, that should probably be a deliberate,
confirmed, and logged action rather than a route that exists "by default."
Flagging this as a decision: do you want case deletion at all, ever, or
should "closed"/"archived" status be the only way to retire a case?

### 4. Document ingestion (copy-in, hash, read-only, dedup, custody event)
**Implemented.** `app/core/ingestion/service.py`: copies the source file
into `originals/<sha256>/<filename>` (content-addressed, so filename
collisions can't overwrite anything), computes SHA-256, sets the copy
read-only at the OS level, rejects exact-duplicate content within the same
case, and writes the `imported` custody event in the same transaction as
the document row.

**Why it matters.** This is the core evidentiary guarantee: what's in the
vault is provably what was imported, with a hash to prove it later, and a
duplicate can't silently create a second, divergent "original."

**Risks / decisions for you.** "Read-only" is an OS file-permission bit,
not a cryptographic or immutable-filesystem guarantee — anyone with
sufficient OS-level access to your account (or an admin/root account) could
still change it back and edit the file. The SHA-256 hash plus the
"Verify integrity now" action are what actually catch that after the fact,
not the permission bit itself. This is disclosed in
`docs/PRIVACY_SECURITY.md` §3 already, but worth restating here plainly.

### 5. Document version tracking
**Implemented.** `app/core/ingestion/versioning.py`: a corrected/reissued
file is ingested as its own independent document (own hash, own file), then
explicitly linked as a new version via `document_version_groups` /
`supersedes_document_id`. Exactly one version per group is current — and
that invariant is enforced by a SQLite partial unique index at the
**database level**, not just application code.

**Why it matters.** "Never overwrite historical documents, preserve
relationships between superseded and current" was an explicit requirement.
The database constraint means even a future bug in application logic can't
silently produce two "current" versions of the same record.

**Risks / decisions for you.** Linking is entirely manual right now (you
choose "link a new version" from a document's page) — the system never
guesses that two files are related. That was a deliberate decision
(`docs/PROJECT_PLAN.md` #12) to avoid false version-matches; the tradeoff
is it only works if you remember to link. Worth deciding whether that's
sufficient for your workflow or whether an assisted "this looks similar —
link as a version?" suggestion should be prioritized earlier than planned.

### 6. Chain-of-custody ledger + integrity verification
**Implemented.** Every document accumulates append-only
`document_custody_events` rows (`imported`, `version_linked`,
`version_superseded`, `hash_verified`, ...), each independently recording
the hash/size/location *as observed at that event*. A "Verify integrity
now" button on each document's page recomputes the hash and logs the
result whether it matches or not.

**Why it matters.** This is the ledger that would let you (or someone
reviewing your evidence) demonstrate a document hasn't been altered since
a specific point in time, not just at import.

**Risks / decisions for you.** Verification is currently manual
(you click the button). Nothing runs it automatically on a schedule.
Decide whether periodic automatic re-verification (e.g., on every app
startup, or weekly) belongs in Phase 2/7, given it's cheap to run and
would catch tampering or disk corruption without you remembering to check.

### 7. Minimal web UI
**Implemented.** Case list/detail, document detail (metadata, custody log,
version history), upload and new-version forms. Server-rendered, no
external CDN assets (verified by grep — see below), loopback-only.

**Why it matters.** Lets you actually use and inspect everything above
without a database client.

**Risks / decisions for you.** It's intentionally minimal — no search, no
filtering beyond what's on the page, no bulk actions. That's in scope for
later phases, not a Phase 1 gap.

### 8. Automated test suite
**Implemented.** 49 tests (1 skipped only because it tests a root-bypass
scenario that doesn't apply when running as root), covering the guardrail,
hashing/read-only/dedup, custody log correctness including a deliberate
tampering scenario, version-linking invariants including a deliberate
attempt to defeat the DB constraint, migration idempotency, and full
HTTP-level flows. Every test runs against a throwaway vault, never a real one.

**Risks / decisions for you.** None outstanding. One honest limitation:
tests run as root in this environment, so the "read-only actually blocks a
write" behavior is verified by checking the permission bits were cleared,
with the "OS actually refuses the write" half of that test skipped here —
it would run for real on your own (non-root) machine.

### 9. Start scripts / run instructions
**Implemented.** `scripts/start.sh` / `start.bat`, `scripts/init_vault.py`,
updated README. Installs only from PyPI, using the pinned dependency list
in `pyproject.toml`.

---

## 1. Are original source files byte-identical forever?

**Yes, within Phase 1's guarantees, and this is verified, not just
designed:**

- Ingestion only ever *reads* the file you select — `copy_into_vault()`
  opens the source for reading and writes a new copy; nothing in the
  ingestion, versioning, or custody code ever opens a source file or a
  stored original in write mode.
- The stored copy is set read-only at the OS level immediately after
  copying (confirmed in the manual smoke test: `-r--------` permissions on
  the actual files on disk).
- A SHA-256 hash is recorded at import and can be re-checked on demand
  (and, per the decision above, could be automated). If a stored file is
  ever altered — whether by a bug, a permission bypass, or disk
  corruption — re-verification will detect and log the mismatch; it will
  not silently accept the new content as correct.
- This was stress-tested directly: `tests/test_custody.py` deliberately
  bypasses the read-only protection, edits the stored file, and confirms
  verification catches it and logs `matches: False` without altering the
  recorded "true" hash.

**What Phase 2 must do to keep this true — and how it will:** extraction
and (Phase 3) OCR read a stored original to produce derived text, but never
write back into `originals/`. Per `docs/DATA_MODEL.md`, extracted text
lives in `document_pages` (a new table, one row per page) and OCR output in
a distinct column on that same table, both clearly separate from the
`documents`/`originals/` storage layer. Concretely, the Phase 2 extraction
service will look like `app/core/extraction/service.py` reading
`vault.root / document.stored_path` and writing only to new database rows —
structurally the same shape as ingestion, which never writes to a source.
I'll apply the same discipline used here: no extraction code opens a
stored original in write mode, and this will get the same kind of
dedicated test coverage (an "extraction never touches the original's hash"
test) before Phase 2 is considered done.

## 2. How will Phase 2 preserve traceability to exact document/page/location?

The schema for this is already fully designed in `docs/DATA_MODEL.md`
(reviewed and approved before Phase 1 coding began) — Phase 2 implements
it, it doesn't redesign it:

- **`document_pages`** (new in Phase 2): one row per page of a document,
  storing that page's extracted text (or OCR text, kept in a separate
  column so the two are never confused) and a `page_number`. This is what
  gives you "which page."
- **`citations`** (new in Phase 2): the reusable "exact source reference"
  primitive — `document_id`, `page_id`, `start_offset`/`end_offset` (or
  `paragraph_index` for DOCX, or a `bounding_box` for image/PDF spatial
  references), and the literal quoted text. This is what gives you "which
  exact location on that page."
- Every later structure that makes a claim — a verified fact (Phase 3.5), a
  timeline event (Phase 4), a relationship-graph edge (Phase 4.5), a binder
  section (Phase 6) — is required to link to at least one `citation`, which
  in turn always resolves back to a specific `document_id` + `page_number`
  + location. There is no path in the designed schema to a "fact" or
  "summary" that doesn't trace to a page.
- AI-generated output (an OCR-derived guess, a suggested date, a generated
  summary) is kept in separate tables (`ai_observations`, `ai_summaries`)
  from human-confirmed material (`verified_facts`), so traceability also
  includes *provenance of confidence* — you can always tell whether
  something was directly cited by a human or machine-suggested and pending
  review.

None of `document_pages` or `citations` exist in the database yet — that's
exactly what Phase 2 adds, additively, via a new migration. Nothing about
the Phase 1 schema needs to change to accommodate them (this is the
lookup-table/extensibility design paying off, per
`docs/DATA_MODEL.md` "Extensibility").

## 3. Is the system still local-first and FERPA-conscious? What data leaves?

**Confirmed by direct inspection of the code, not just by design intent:**

- `app/config.py:30` — the server's default bind address is `127.0.0.1`
  (loopback only). Nothing in the codebase overrides this to `0.0.0.0`.
- Runtime dependencies (`pyproject.toml`) are FastAPI, Uvicorn, SQLAlchemy,
  Alembic, Jinja2, python-multipart, pydantic-settings — all local-execution
  libraries. `httpx` (an HTTP client) is a *test-only* dependency, used
  solely by `TestClient` to call the app in-process during `pytest` runs;
  it is not imported anywhere in `app/`.
- A repository-wide search for outbound-network patterns
  (`requests.`, `httpx.`, `urlopen`, raw sockets, hardcoded `http(s)://`
  URLs) inside `app/` returns nothing.
- A search for telemetry/analytics SDK names (Sentry, Mixpanel, Segment,
  PostHog, etc.) returns nothing.
- The web UI's static assets are all self-hosted; there are no CDN, Google
  Fonts, or other external asset references in `app/web/`.

**What leaves the local environment: nothing, by the application itself.**
The only way case data could ever reach GitHub is if you personally moved
vault files into the git-tracked repo directory and committed them — and
the guardrail in item 1 specifically exists to make that structurally hard
to do by accident (the app won't even run against a vault path inside a
git repo).

**What Phase 2/3 will introduce, and the constraint that stays fixed:**
OCR (Phase 3) will run via Tesseract, a local binary — not a cloud vision
API. No cloud AI/LLM is used anywhere in the currently planned phases; if
that's ever proposed later (`docs/PROJECT_PLAN.md` Phase 8, explicitly
optional and off by default), it would need its own explicit, visible
opt-in and would not change this default.

## 4. Planned design: timelines, duplicates, conflicting dates, version relationships

Two of these are already implemented (Phase 1); two are designed but not
yet built (Phase 4/5). Here's where each actually stands:

**Duplicate handling — implemented now.** Exact-content duplicates (same
SHA-256 hash) within a case are rejected at ingestion with a clear error
naming the existing document; they are never silently discarded *or*
silently re-imported as a second copy. The same content in *different*
cases is allowed (a document can legitimately belong to more than one
matter). **Not yet handled:** near-duplicates — the same letter scanned
twice with slightly different image compression, or a redacted vs.
unredacted version. Per `docs/PROJECT_PLAN.md` decision #5, that's
explicitly deferred to manual tagging (you'd flag them yourself); no
automatic "these look similar" detection is planned before Phase 8, if ever.

**Version relationships — implemented now.** Covered in detail in the
checklist above (item 5). This is done, not just planned.

**Chronological timeline — designed, not yet built (Phase 4).** The design
(`docs/DATA_MODEL.md`, `docs/ARCHITECTURE.md` §3.6-3.8) has the timeline
built entirely on `verified_facts`, never on raw extracted text directly —
a timeline event links to one or more verified facts via
`timeline_event_facts`, and each of those facts already carries its own
citation and a confidence level (`certain`/`probable`/`uncertain`). Dates
can be *suggested* by an extraction/parsing step, but a suggestion is an
`ai_observation` until a human reviews and promotes it — it cannot appear
on the timeline as a suggestion; it's either not on the timeline yet, or
it's a confirmed fact. One near-term implication worth deciding now: the
`documents.document_date` column already exists in the Phase 1 schema
(unused so far) but the ingestion UI doesn't currently let you set it
manually. Since manual date entry doesn't depend on any extraction work,
I'd suggest adding a simple "document date" field to the upload form early
in Phase 2 (before automated date-suggestion exists) so you can start
building timeline-ready data immediately rather than waiting for Phase 4.
Flagging this as a decision for you: do you want that pulled forward, or
is it fine to wait?

**Conflicting dates / conflicting records — designed, not yet built
(Phase 5).** The design has a `conflicts` table with `conflict_facts`
linking ≥2 `verified_facts` (not raw text) to each other, with a note —
explicitly a human-flagged workflow with a side-by-side comparison UI, not
automatic contradiction detection. The reasoning from
`docs/PRIVACY_SECURITY.md` §9 carries through here: the system surfaces
that two confirmed facts disagree and lets you (or your attorney)
characterize why, rather than the app itself asserting which one is
"correct" or that a conflict is significant.

**What this means for the order of Phase 2 work:** Phase 2 (extraction,
search) doesn't touch timeline, conflicts, or the relationship graph at
all — those are Phase 3.5/4/4.5/5 per the approved roadmap. Phase 2's job
is narrower: get text out of documents with page-level citations, and make
it searchable. I'm not proposing to reorder the roadmap, just flagging the
one small addition above (manual `document_date` entry) since it's cheap
and has no dependency on anything else in Phase 2.

---

## Decisions consolidated for you as owner

1. Is `~/FERPA-Evidence-Vault` the right default vault location, or do you
   want a different one? *(carried over from Phase 0 planning, still open)*
2. Should case deletion exist at all in this app, ever — or should
   "archived" status be the only retirement path?
3. Should integrity re-verification run automatically (e.g. on startup or
   on a schedule) rather than only when you click "Verify integrity now"?
4. Is manual (never automatic) version-linking sufficient for your
   workflow, or should an assisted "this looks like a version of..."
   suggestion be pulled earlier than currently planned?
5. Should a manual `document_date` field be added to the Phase 2 ingestion
   form now, ahead of automated date-suggestion in later phases, so
   timeline-ready data can start accumulating sooner?

None of these block Phase 2 from starting technically — they're judgment
calls about workflow and risk tolerance that are yours to make, not
technical prerequisites.
