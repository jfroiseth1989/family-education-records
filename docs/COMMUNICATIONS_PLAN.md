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
3. ✅ Manual `.eml` upload → `communications`/`communication_attachments`
   + custody events. `app/core/extraction/email.py` gained
   `parse_message()`, a second entry point alongside the pre-existing
   `extract()` (left untouched -- still serves the Document extraction
   pipeline), returning a `ParsedEmailMessage` with full structured
   headers/addresses/body-text-and-html-separately for `Communication`.
   `app/core/communications/ingestion.py::import_eml_file()` mirrors
   `ingest_document()`: hash, copy read-only, custody event, in one
   transaction; attachments get the same treatment one level down,
   always landing `review_status="pending"` (classification is Step 5).
   Dedup: primarily `(account_id, message_id)`, falling back to
   `(account_id, sha256_hash)` when no Message-ID is present -- checked
   in application logic, not only the schema's partial unique index.
   Upload UI lives on the existing `/communications` hub (a student
   picker + file input) plus a new `/communications/email/{id}` detail
   page and an "All Communications" list -- verified working with zero
   Yahoo accounts connected, proving the independence requirement.
   Thread reconstruction is still Step 4; nothing here groups messages.
4. ✅ Thread reconstruction + thread view. Pure grouping algorithm in
   `app/core/communications/thread_grouping.py::group_messages()`, in the
   approved precedence order (Message-ID as a lookup target, then
   In-Reply-To, then References -- all three build one linkage graph
   via connected components, since References is RFC 5322-defined to be
   a superset of what In-Reply-To names; only a message with *none* of
   the three falls back to conservative subject+participants+date-
   proximity matching, and only against other equally headerless
   messages). A component of size 1 stays unthreaded -- no
   `communication_threads` row, no broken thread link in the UI.
   `app/core/communications/thread_rebuild.py::rebuild_threads()` is the
   DB-facing reconciliation layer: a full, deterministic rebuild over
   every non-deleted communication on each call, called automatically at
   the end of `import_eml_file()`. Idempotent (reuses existing
   `thread_id`s, no row churn on a no-op rerun); handles out-of-order
   imports and a reply imported before its parent (both fall out of
   recomputing from full current state rather than incremental
   patching); when a later message links two previously separate
   threads, the lower `thread_id` is kept canonical and the other
   thread row is deleted once its members are reassigned -- the
   underlying `Communication` rows and their custody events are never
   touched by any of this, only the `thread_id` column and the derived
   `communication_threads` row. New `/communications/threads` (list) and
   `/communications/threads/{id}` (chronological detail) routes;
   `communication_detail.html` gained a "View Thread" link shown only
   when `thread_id` is set. No new migration (the `communication_threads`
   table already existed from Step 1).
5. ✅ Attachment educational-record detection + single-item attachment
   review + "Add to Documents" + document↔email linking + cross-email
   duplicate-document detection. `app/core/document_type_suggestion.py`
   gained trigger coverage for the original Phase 1 DocumentTypes
   (Evaluation, 504 Plan, Correspondence, Discipline, Attendance,
   Grades, Medical, Legal Filing, Audio Transcript), with
   "Reevaluation"/"Eligibility Determination" as trigger synonyms for
   Evaluation per §1 decision 2 -- no new DocumentType rows added.
   `app/core/communications/attachment_classification.py` reuses that
   same matcher (best-effort native text, filename-only on an
   unsupported/unreadable format -- never a forced or crashing
   classification) and is run automatically in `_store_attachment()`,
   filling in only the advisory `is_educational_record_candidate`/
   `suggested_document_type_id` columns; `review_status` stays
   `"pending"` until a human decides.
   `app/core/communications/attachment_metadata.py` turns a
   Communication's already-structured From/sent/received fields into
   Source/Date-Received/Notes suggestions -- deliberately not built on
   the regex-based `document_source_suggestion.py`/`document_date_suggestion.py`,
   since structured data is strictly higher-confidence than re-deriving
   it from text.
   `app/core/communications/promotion.py::promote_attachment_to_document()`
   reuses `ingest_document()` unchanged, reading the attachment's own
   preserved copy as its source; on `DuplicateDocumentError` it links to
   the existing Document via `CommunicationDocumentLink` instead of
   raising a user-facing failure, writes a `communication_attachment_linked`
   custody event on the Document ledger and an `attachment_added_to_documents`
   event on the Communication ledger, and is idempotent (a repeat
   promotion finds the existing link and writes nothing further).
   `exclude_attachment()`/`leave_attachment_with_email()` record the
   other two review outcomes without ever creating a Document.
   New `/communications/attachments/{id}/review` (view/edit-suggestions/
   Add to Documents/Exclude/Leave with Email), `/review`'s file-serving
   sibling, `/add-to-documents`, `/exclude`, and `/leave-with-email`
   routes; `communication_detail.html` and `document_detail.html` gained
   bidirectional provenance sections (linked Documents from an email;
   originating emails for a Document, supporting more than one).
   Bulk attachment review is still Step 11 -- this step only established
   correct single-attachment behavior for it to reuse.
6. ✅ `communication_text_fts` (new, independent FTS5 table) + Communications
   search/filter UI. Indexes `subject` + parsed plain-text `body_text`
   only. Kept entirely separate from `document_text_fts` and
   `annotation_notes_fts` -- a Communications result can never be
   ambiguous with either. Sync via unconditional insert/delete/update
   triggers, matching `document_pages_fts_au`'s exact pattern -- an
   earlier draft tried `WHEN deleted_at IS NULL`-guarded triggers to skip
   indexing soft-deleted rows and that **corrupted the FTS5 index**
   (reproduced directly: an UPDATE that didn't even touch subject/body,
   e.g. only `thread_id` changing during thread rebuild, silently broke
   `MATCH` for that row with no error raised). Fixed by indexing every
   row unconditionally and excluding soft-deleted communications at
   query time instead (`c.deleted_at IS NULL`), the same division of
   responsibility Document search already uses -- see the migration's
   docstring for the full account.
   `app/core/indexing/communications_search.py::search_communications()`
   uses FTS5 (via the same `_quote_as_phrase()` safe-quoting helper
   Document search already relies on -- untrusted input is always
   wrapped as a literal phrase, never raw FTS5 syntax) only for the free-
   text query; every structured filter (sender, recipient, CC, subject,
   date range, has-attachments, case, threaded/unthreaded) is a plain
   SQL condition, including `json_each()` over the JSON-array
   `to_addresses`/`cc_addresses` columns for recipient/CC. New
   `/communications/search` route/page; result rows link to the existing
   communication detail page and, when threaded, the thread view.
7. ✅ Timeline suggestions from communications (approve/edit/reject),
   linked back to source email. A `Communication` never automatically
   becomes a `VerifiedFact`/`TimelineEvent` -- only a pending
   `AiObservation`, reusing the exact same review lifecycle Document
   text already goes through (Phase 3.5), not a parallel one.
   Schema: one additive column each on `ai_observations`/`verified_facts`
   (`communication_id`, nullable, UNIQUE on the observation side) --
   `citations` (which requires a non-null `document_id` plus page/
   offset/bounding-box fields) is untouched and unused for this path, on
   purpose: a Communication has no citable page/offset span, and forcing
   one through that table would be exactly the "fake Document
   relationship" the plan warned against.
   `app/core/communications/timeline_suggestions.py::generate_timeline_suggestion()`
   builds the fixed candidate `Email received from <sender> regarding
   "<subject>" on <date>.` from only `from_display_name`/`from_address`/
   `subject`/`sent_at`/`received_at` -- never body text -- and only when
   a reliable date exists (`received_at` preferred, `sent_at` fallback,
   which one used recorded in the audit log); returns `None` (no
   suggestion) otherwise, and is idempotent (at most one `AiObservation`
   per communication, checked regardless of status, backed by the
   schema's UNIQUE constraint). Runs automatically at import time.
   `app/core/facts/service.py::promote_observation()`/`reject_observation()`
   are reused completely unmodified for Approve/Edit+Approve/Reject
   (aside from one additive line in `promote_observation()` copying
   `communication_id` onto the resulting fact -- a no-op for every
   Document-sourced observation). The candidate deliberately reuses the
   existing "date" `FactType` (not a new one) specifically so a promoted
   suggestion is immediately eligible to anchor a real `TimelineEvent`
   through the existing, completely unmodified
   `list_date_source_candidates()`/`create_timeline_event()` -- proving
   Communication-sourced facts are fully interoperable with the existing
   Timeline UI without a single change to `app/core/timeline/`.
   `communication_detail.html` gained a Timeline Suggestion panel
   (Pending/Accepted/Rejected status, inline Approve/Edit+Approve/Reject
   posting to the existing Facts routes); `facts_review.html` gained a
   "View Source Email" link wherever `communication_id` is set.
8. ✅ Investigated `.msg` and other saved-email formats; implemented only
   the one that reliably clears the evidence/provenance bar (`.mbox`).

   **`.msg` (Outlook/MAPI) -- investigated, not implemented.** Two
   candidate Python libraries were evaluated:
   - `extract-msg`: feature-complete (From/To/CC/BCC, Subject, sent
     date, Message-ID, In-Reply-To/References when present, plain-text
     body, HTML body, raw transport headers when preserved by the
     original `.msg`, attachments as original bytes), actively
     maintained, pure Python (no native/system dependency). Disqualified
     anyway: it is **GPL-3.0-licensed**. FERChronos is described in its
     own `pyproject.toml` as a private, local-first application intended
     for eventual distribution to non-technical families -- bundling a
     GPL-3.0 dependency would put the *entire distributed application*
     under GPL-3.0's copyleft/source-disclosure obligations, a real,
     hard-to-reverse legal/business consequence, not a coding one. This
     alone is disqualifying regardless of field-coverage completeness.
   - `python-oxmsg`: properly MIT-licensed (shares an author with the
     already-depended-on `python-docx`), but confirmed pre-1.0/alpha via
     its own README, with To/CC/BCC, HTML body, raw headers, and
     Message-ID extraction not yet documented as supported. Not reliable
     enough today to trust for evidentiary provenance.

   Per this step's own explicit permission ("if it cannot [meet the
   requirements reliably], document the limitation and do not implement
   a compromised parser just to check the feature box"), `.msg` import
   is **not implemented**. This is a reasoned, written decision, not an
   oversight -- revisit if `python-oxmsg` matures to 1.0 with full field
   coverage, or if FERChronos's distribution model changes such that
   GPL-3.0 is no longer a blocker.

   **Other formats -- classified, not all implemented:**
   - `.mbox` -- **implemented** (see below). Python stdlib-only
     (`mailbox.mbox`), no licensing risk, no native dependency; each
     contained message is a genuine, unmodified RFC822 original (an
     mbox archive only concatenates real messages -- it does not
     transform, normalize, or reconstruct any one of them), so the
     entire existing, already-battle-tested `.eml` pipeline applies
     without a second parser.
   - `.mbx` -- **unsupported for now.** Non-standardized (multiple
     incompatible legacy formats have used this extension), no reliable
     actively-maintained library exists. The upload route accepts the
     `.mbx` extension alongside `.mbox` only because some mail clients
     export true mbox-format archives under a `.mbx` name; this is not
     support for the genuinely different legacy `.mbx` formats.
   - `.oft` -- **unsupported / not applicable.** An Outlook *template*
     file, not an actual sent/received message -- it fails the
     Communications model's basic premise (a real transmitted
     communication) regardless of parser availability.
   - `.emlx` -- **unsupported for now.** Apple Mail's internal per-
     message storage format (an RFC822 body plus an XML plist trailer);
     technically parseable with custom handling, but not a format users
     deliberately "Save As" or export -- it is Mail.app's own internal
     storage, encountered only by reaching into `~/Library/Mail`
     directly. Low practical value relative to the added parsing
     complexity; revisit only if a real user need for it surfaces.

   **`.mbox` import -- implemented.**
   `app/core/communications/mbox_import.py::import_mbox_file()` opens
   the archive read-only via stdlib `mailbox.mbox()`, extracts each
   entry's raw RFC822 bytes unchanged to a throwaway temp file, and
   calls `import_eml_file()` once per message -- zero new parsing logic.
   `import_eml_file()` gained two additive, defaulted parameters
   (`import_method="manual_upload"`, `custody_details=None`) so every
   pre-Step-8 caller is unaffected; `.mbox`-derived messages pass
   `import_method="mbox_import"` (a new, distinct value from
   `"manual_upload"` -- the message bytes are exactly as genuine as a
   individually-saved `.eml`, but provenance should still record that a
   human handed FERChronos an archive rather than one file) and
   `custody_details` recording the source archive's filename and its own
   SHA-256 hash. A message that duplicates one already imported (within
   the same archive or a prior import) is skipped via the existing
   `DuplicateCommunicationError` path rather than aborting the whole
   batch, so the same archive can safely be re-imported after new
   messages are appended to it. New `/communications/upload-mbox` route
   (redirects to the Communications home with an imported/duplicate/
   unparseable-entry count summary, since an archive can contain many
   messages and there is no single detail page to redirect to) and a
   matching upload form on `communications_home.html`, alongside the
   existing `.eml` form.

   **PDF/screenshot emails -- deterministic "looks like correspondence"
   suggestion only, per the standing decision.** A PDF export, print, or
   screenshot of an email remains a normal Document, never a
   `communications` row -- it is not an RFC822/MAPI original.
   `app/core/document_type_suggestion.py` gained one new compound
   trigger on the existing "Correspondence" type: a raw (non-`_t()`)
   regex detecting the classic printed/exported header block --
   `From:` ... `Sent:` ... `To:` ... `Subject:` co-occurring in that
   order within a bounded gap (so unrelated later text mentioning all
   four words can't false-positive, and a different order doesn't
   match). This only ever produces the existing, already-advisory
   Correspondence suggestion on a normal Document -- it does not import
   anything as a Communication and does not otherwise change any
   PDF/image ingestion behavior.

   Threading was not weakened to accommodate any of this: `.mbox`
   messages carrying real Message-ID/In-Reply-To/References headers
   thread through the normal header-based algorithm exactly like `.eml`;
   messages without them fall back to the same conservative headerless
   matching every other headerless message already uses. Nothing
   fabricates a header that was not actually present in the original
   message.

**C — Automatic Yahoo import**
9. ✅ Read-only IMAP connectivity (app-password auth) + folder listing +
   search-by-criteria, returning counts/previews only. FERChronos's
   first live network integration -- see docs/PRIVACY_SECURITY.md §2.1
   for the full outbound-network-exception writeup.

   `app/core/communications/imap_client.py` is a narrow, protocol-level
   `ImapClient` with no `Communication`/`CommunicationAccount`
   dependency of its own: connect/authenticate, list folders, search
   (always via a fresh read-only `SELECT`), a bounded metadata-only
   preview fetch, and logout -- there is no method for STORE, COPY,
   MOVE, EXPUNGE, DELETE, APPEND, or any flag-mutating command; those
   verbs simply aren't part of the class, not merely unused. Preview
   fetches use `BODY.PEEK[HEADER.FIELDS (...)]` (never a bare `BODY[...]`,
   which would mark `\Seen`), so browsing never alters mailbox state on
   Yahoo's server. Talks to the mailbox through a small `_ImapTransport`
   protocol rather than `imaplib` directly, resolved against a
   module-level default at call time (not bound as an ordinary default
   argument) specifically so tests can substitute an in-process fake
   transport with zero real network access and zero real Yahoo account
   -- see `tests/test_communications_imap_client.py::FakeImapTransport`.
   `app/core/communications/imap_service.py::open_connection()` is the
   one bridge from a `CommunicationAccount` row to a live, authenticated
   client: retrieves the app password via the existing
   `credentials.get_credential()` (Step 2, unchanged), raising
   `ImapCredentialUnavailableError` without ever touching the network if
   no credential is currently stored.

   Every error path is mapped to one of five typed exceptions
   (`ImapCredentialUnavailableError`/`ImapAuthenticationError`/
   `ImapNetworkError`/`ImapTimeoutError`/`ImapMailboxAccessError`), each
   with a fixed, safe message string -- never built from a raw server
   response or from any credential -- so nothing an exception carries can
   ever leak the app password into a rendered page, a log, or a URL. A
   failed test-connection or browse attempt never disconnects, deletes,
   or otherwise modifies the stored `CommunicationAccount` row.

   Search criteria (sender/recipient/cc/subject/keywords/date range) map
   directly to IMAP's own `FROM`/`TO`/`CC`/`SUBJECT`/`TEXT`/
   `SENTSINCE`/`SENTBEFORE` search keys -- matched server-side, never by
   downloading messages and filtering locally. Every text value is
   quoted via `_quote_astring()`: control characters (in particular
   CR/LF, which could otherwise inject a second protocol line into the
   command stream) are stripped, backslash/double-quote are escaped, and
   non-ASCII is replaced with `?` (this narrow client does not implement
   IMAP literal/charset framing, so a search term with non-ASCII
   characters may not match exactly -- a documented limitation, not a
   silent correctness bug: it degrades to "no match," never to executing
   something other than the intended command). UID sets passed to FETCH
   are validated against a strict digits-only pattern before ever being
   interpolated into a command string.

   **No `has_attachments` search filter.** Yahoo/IMAP has no native,
   reliable server-side "has an attachment" search key, and
   approximating one would require fetching each candidate message's
   full body or structure across the whole mailbox -- exactly the
   expensive, mailbox-wide download this step is required to avoid. Per
   this step's own "be conservative about unsupported criteria"
   instruction, this filter is not implemented; the same applies to an
   attachment indicator on preview rows (also omitted, for the same
   reason -- BODYSTRUCTURE inspection was considered and set aside as
   more parsing-surface risk than an evidentiary tool should carry for a
   preview-only feature).

   Search is always bounded: `search()` returns Yahoo's true match count
   (`total_matched`, cheap -- SEARCH returns UIDs, not messages) but only
   ever fetches preview metadata for a capped page (`limit`, default 25,
   hard-capped at 200; `offset` for paging) -- never more messages than
   that page regardless of how large the mailbox-wide match count is.
   Results are ordered newest-first, matching how the rest of this
   application always orders communications.

   **UID scope.** An IMAP UID is only unique within its folder --
   `ImapMessagePreview` always carries `folder` and `uid` together, and
   nothing in this application treats a UID alone as a durable message
   identity. This is deliberately the shape a future import step will
   need to key an "already imported this message" check off
   `(account, folder, uid)`.

   **Step 10 architecture boundary.** `ImapClient.fetch_raw_message(folder,
   uid)` exists so a future bulk-import engine can request one message's
   complete, unmodified RFC822 bytes without this module changing --
   also via `BODY.PEEK[]`, so even a raw fetch never marks a message
   read. No Step 9 route calls it, and nothing in Step 9 uses its result
   to write to the vault, `communications`, or any custody ledger.

   UI: `/communications/{account_id}/test-connection` (POST -- connects,
   counts folders, disconnects, shows a pass/fail banner on the
   Communications home page) and `/communications/{account_id}/browse`
   (GET -- lists real folders dynamically, never a hard-coded
   Inbox/Sent/Trash guess; folder attributes like `\Sent`/`\Trash`/
   `\Junk`/`\Drafts`/`\All` are shown when the server provides them
   rather than relying on display-name guessing alone). The browse page
   states prominently, in its own words, "No email has been imported
   yet" and that results are Yahoo mailbox search results only --
   distinct from evidence already stored in FERChronos.
10. ✅ Bulk import engine (batched/resumable/cancellable/retry-safe) + the
    pre-import review screen + batch case assignment + progress/status UI.

    Reuses `communication_import_batches`/`communication_import_batch_items`
    (schema from Step 1, unpopulated until now) as the resumable work
    queue, and reuses the entire existing `.eml` ingestion pipeline
    unchanged for the actual import -- no parallel Yahoo-specific
    evidence path was built. `CommunicationImportBatchItem` gained
    `mailbox_folder` (additive migration `9f6d90a557ce`; the column was
    simply missing before this step) alongside `mailbox_uid`, with the
    unique constraint widened to `(batch_id, mailbox_folder,
    mailbox_uid)` -- Step 9 already established a UID is only unique
    within its own folder, so a batch item's durable identity always
    needs both. `import_eml_file()` gained three more additive,
    defaulted parameters (`account_id`, `mailbox_folder`, `mailbox_uid`)
    on top of Step 8's `import_method`/`custody_details` -- every
    pre-Step-10 caller is unaffected.

    `app/core/communications/imap_import.py::import_one_imap_message()`
    is the one place raw bytes cross from "on Yahoo's server" to
    "handed to `import_eml_file()`": fetches via
    `ImapClient.fetch_raw_message()` (Step 9's `BODY.PEEK[]`-based
    architecture boundary, first real caller), writes to a throwaway
    temp file, imports with `import_method="imap_sync"`. Preserves
    SHA-256/read-only-vault-copy/attachment-preservation/duplicate-
    detection/threading/attachment-classification/timeline-suggestion/
    FTS5-indexing exactly as every other import path does, because it
    *is* every other import path -- nothing here duplicates that logic.

    `app/core/communications/import_batches.py` is pure database
    bookkeeping (`create_batch()`/`request_cancel()`/`resume_batch()`/
    `retry_failed_items()`/`count_remaining()`) with no IMAP dependency
    of its own -- `create_batch()` persists the *entire* selected work
    queue as `pending` items in one transaction before any processing
    begins, which is what makes the batch resumable at all. Requires a
    case/student up front (`BatchValidationError` otherwise); no
    per-message override UI was built this step, per the plan's own
    "don't overbuild an override interface that belongs in the bulk-
    review step" guidance -- the architecture (a nullable per-item path
    to a different case) is not foreclosed, just not built yet.

    `app/jobs/import_worker.py` is a second, independent daemon thread
    (same pattern as the Phase 3 OCR worker -- single bounded worker,
    fresh `Session` per pass, WAL already covers concurrent access) but
    deliberately not a modification of it: OCR and Communications are
    unrelated domains. Two considered departures from the OCR pattern,
    not oversights:
    - **No crash-recovery sweep.** `OcrJob` has no persisted per-unit
      progress, so an interrupted job must be force-failed at startup.
      A `CommunicationImportBatchItem` is committed individually as it
      completes, so a batch left `running` by a crash is already just
      as resumable as any other in-progress batch -- `claim_next_batch()`
      treats `running` as claimable exactly like `pending`, and the very
      next poll after restart continues it with zero special handling.
    - **Cancellation** (no OCR precedent at all -- confirmed absent by
      grep) is a plain `status` check refreshed from the database
      between every item, written by `request_cancel()` from an
      entirely different request thread; whatever is `pending` when
      observed stays `pending` forever, never rolled back, never marked
      `failed`.

    Per-item error isolation matches this step's explicit split: fetch
    failure / malformed RFC822 / attachment parse-or-storage failure /
    a transient network blip on *one* message all mark just that item
    `failed` and the batch continues; only `ImapAuthenticationError`/
    `ImapCredentialUnavailableError` (a real account-level problem --
    retrying per-item would be pointless, and for a rejected password,
    actively harmful) stop the batch entirely with every not-yet-
    attempted item left untouched `pending`, surfaced as `status="failed"`
    with a Resume control. One commit per item, never one commit for
    a whole batch -- a crash mid-item loses at most that one item
    (rolled back, still `pending`), never anything already committed.

    UI: the browse/search results table (Step 9) gained per-row
    checkboxes, select-all, and a required student selector, wrapped in
    a form posting to the new `/communications/{account_id}/import-batches`
    route -- browsing/searching itself still requires no student and
    still imports nothing; only pressing "Start Import" (creating the
    batch) does anything, and even that only writes rows -- the actual
    Yahoo fetches happen afterward, entirely in the background worker.
    New `/communications/import-batches/{batch_id}` status page (plain,
    manually-refreshed -- deliberately no auto-refresh JavaScript, both
    mirroring the OCR jobs page's own precedent and keeping "no
    background polling" true in spirit as well as letter) with
    Cancel/Resume/Retry-Failed controls shown only when each is
    meaningful for the batch's current status, plus a "Recent Import
    Batches" section on the Communications home page.
11. ✅ Bulk attachment-review screen, built on Step 5's logic.

    Pure orchestration -- `app/core/communications/bulk_attachment_review.py`
    never classifies, never computes a metadata suggestion, and never
    decides duplicate-vs-new-Document on its own. Every actual promotion/
    exclude/leave-with-email decision still runs through Step 5's
    unmodified `promote_attachment_to_document()`/`exclude_attachment()`/
    `leave_attachment_with_email()`, called once per attachment. The one
    piece of read-only logic this step adds itself,
    `_existing_document_for()`, exists only so a listing/preview can say
    "this will link an existing Document" *before* anything commits --
    mirroring `ingest_document()`'s own `(case_id, sha256_hash)` rule
    without ever calling the real ingestion path just to discard the
    result (unsafe: it writes to the vault as a side effect a DB
    rollback can't undo).

    `list_review_attachments()` is a single filterable query (batch,
    student/case, suggested type, review status, recognized/
    unrecognized/duplicates, sender, subject, date range) -- "recognized"
    and "unrecognized" need no new classification signal at all:
    `is_educational_record_candidate` already collapses "no trigger
    matched," "ambiguous (several types matched)," and "unreadable
    format" into the same `False`, exactly the union Step 11's own
    safety requirement needs. `list_recognized_pending_attachments()` is
    the literal "Add all recognized" candidate set --
    `review_status == "pending"` and `is_educational_record_candidate`,
    nothing else -- which by construction already excludes unsupported/
    unreadable/ambiguous files, already-reviewed attachments, and
    already-excluded/left-with-email attachments, without a single
    additional check.

    Bulk actions (`bulk_add_to_documents()`/`bulk_exclude()`/
    `bulk_leave_with_email()`) process attachments one at a time with
    real per-item isolation: each item gets its own `db.commit()` (or
    `db.rollback()` on failure) before the loop continues, so one
    attachment's unexpected failure can never undo an earlier
    attachment's already-committed success, and a partial bulk failure
    never leaves a half-written `Document`/link row behind (the
    rollback always happens before recording that item as failed). An
    attachment no longer `pending` when its turn comes is recorded as
    `already_reviewed`, not `failed` or silently skipped -- what makes
    resubmitting the exact same bulk selection safely idempotent.
    Bulk-accepted suggested metadata is recorded with
    `field_provenance="suggested"` for each field actually used -- the
    same value the single-attachment review page itself records when a
    human submits its pre-filled defaults untouched -- so a bulk-
    accepted default is indistinguishable in the audit trail from a
    human individually accepting that same default. Per-item metadata
    edits before promotion reuse the existing single-attachment review
    page directly (linked from every row) rather than a second edit UI.

    "Add all recognized" is two-phase and never trusts a client-
    submitted attachment list: the first submission only computes and
    shows `preview_bulk_promotion()`'s counts (will-add / will-link /
    not-processable) and commits nothing; only a second submission with
    `confirmed=1` re-queries `list_recognized_pending_attachments()`
    *fresh* and actually promotes -- which is also what makes a
    duplicate confirm-click safe (anything no longer eligible by then
    simply isn't in the recomputed set). Scoped by `batch_id`/`case_id`
    only, regardless of whatever narrower display filters (sender/
    subject/suggested-type) the page happened to be showing --
    "recognized" always means every eligible attachment in that batch/
    student, not an accidental subset of the current view.

    New `/communications/attachments/review` (GET, list+filters) and
    `/communications/attachments/review/{add-selected,
    add-all-recognized, exclude-selected, leave-selected}` (POST) routes
    -- all render the same list template back with a result summary
    rather than redirecting, matching the single-attachment review
    page's own convention. The import-batch status page gained a
    "Review Attachments" link (shown once a batch has imported anything)
    scoped to that batch via `?batch_id=`; the Communications home page
    gained a general, unscoped link to the same page.
12. ✅ Final integration hardening: disconnect/reconnect correctness,
    credential-failure handling, cross-feature provenance audit, network
    boundary audit, and documentation.

    Not a new feature family -- every piece of this step closes an
    integration gap between Steps 9-11's already-implemented pieces, or
    adds tests proving a gap already didn't exist.

    **Account reactivation on reconnect** is the one real behavior
    change. `connect_yahoo_account()` (`app/core/communications/accounts.py`)
    now looks for an existing `CommunicationAccount` row matching the
    same `provider`/`email_address` (case-insensitively) before creating
    a new one; if found, it reactivates that row in place (new
    credential stored under the same `credential_ref`, `status` back to
    `"connected"`, `connected_at` refreshed, `disconnected_at` cleared)
    instead of forking a second logical account. This matters for more
    than tidiness: `Communication.account_id` is exactly what Step 10's
    duplicate-import detection scopes on
    (`ingestion.py::_find_duplicate_communication`, and the DB-level
    `uq_communication_account_message_id` partial unique index), and
    every existing batch/attachment/provenance row's `account_id`
    foreign key already points at the original account. Forking a new
    row on reconnect would have silently narrowed that dedup scope to
    "since the most recent reconnect" -- re-selecting an
    already-imported message after a disconnect/reconnect cycle would
    have created a second `Communication` row for it rather than being
    recognized as a duplicate, without deleting or corrupting anything
    already there. Reactivation is covered directly
    (`tests/test_communications_accounts.py`) and at the full
    batch-import-worker level
    (`test_reconnect_preserves_account_scoped_duplicate_detection` in
    `tests/test_communications_step12_integration.py`), which reconnects
    a disconnected account and proves re-importing the same message
    still resolves to the one existing `Communication` row, not a
    second one.

    **Unfinished-batch safety after disconnect.** `resume_batch()`/
    `retry_failed_items()` (`app/core/communications/import_batches.py`)
    are now no-ops whenever the batch's account isn't currently
    `connected` -- previously, clicking Resume on a batch whose account
    had been disconnected would still flip it to `pending`, only for the
    worker to immediately fail it again once it discovered the missing
    credential (safe, since `open_connection()` already refuses to open
    a socket with no stored credential, but a pointless, confusing
    round-trip). The batch status page
    (`communication_import_batch.html`) now checks `batch.account.status`
    directly and only offers Resume/Retry-Failed when the account is
    actually connected, showing a plain explanation and a link back to
    reconnect otherwise -- satisfying "show a clear message" without
    needing a new persisted batch state.

    **Missing/revoked keyring credential.** This was already handled
    correctly by Step 9's design (`imap_service.py::open_connection()`
    checks `get_credential()` for `None` *before* ever constructing a
    socket, and every route already converts
    `ImapCredentialUnavailableError` into a fixed, safe message) --
    Step 12 adds direct test coverage for the specific scenario the spec
    calls out (the database still says `"connected"` but the OS keyring
    entry was deleted outside FERChronos, not via Disconnect) and
    surfaces it proactively rather than only reactively: `_account_rows()`
    (`app/api/communications.py`) does a local, network-free
    `get_credential()` check on every Communications home-page load and
    renders "Credential unavailable" instead of a misleading "Connected"
    badge, with a hint that already-imported evidence is unaffected
    either way.

    **Yahoo-side revocation mid-batch.** Already correctly isolated by
    Step 10's worker design (`ImapAuthenticationError` mid-loop stops the
    batch and leaves that item `pending`, but never touches an already-
    committed item from earlier in the same batch) -- Step 12 adds
    `test_auth_revocation_mid_batch_preserves_earlier_successful_imports`
    to prove it directly: a batch with two items where the second raises
    an auth failure ends with the first message fully imported,
    unmodified, and not soft-deleted, while the second stays `pending`
    and the batch is `failed`, not partially rolled back.

    **Account status UI** (`communications_home.html`) now distinguishes
    "Connected," "Credential unavailable," and "Disconnected" instead of
    a binary connected/disconnected badge, and every disconnected or
    credential-unavailable row explicitly states that its previously
    imported evidence remains local and fully usable. Language
    throughout was reviewed to avoid implying a continuous live
    connection (there still isn't one -- see §16/Privacy doc).

    **Cross-feature provenance audit and orphan/reference integrity**
    (`tests/test_communications_step12_integration.py`) trace one
    realistically imported email-with-attachment through every
    relationship named in the spec, in both directions, always via a
    real FK/link: Email↔Thread, Email↔Attachment, Email/Attachment↔
    Document (via `CommunicationDocumentLink`, not filename matching),
    Email↔Timeline Suggestion (`AiObservation.communication_id`), Import
    Batch↔Communication (`CommunicationImportBatchItem.communication_id`),
    and Communications Search↔Email -- both at the database layer and by
    asserting the actual rendered HTML contains the real link/URL, not
    reconstructed text. Separate tests confirm disconnect orphans
    nothing and reconnect mutates no existing provenance row.

    **Network boundary audit.** A dynamic test
    (`test_local_evidence_routes_never_open_an_imap_connection`) patches
    `ImapClient.connect_and_authenticate()` to raise if it's ever called,
    then exercises every local, already-imported-evidence route (home,
    email detail, thread, threads list, search, attachment review,
    single-attachment review, batch status, Document detail, facts
    review, timeline) and confirms none of them trip it -- proving by
    construction, not by inspection, that only Test Connection/Browse/
    Import ever reach the IMAP layer.

    **Docs.** `docs/PRIVACY_SECURITY.md` gained §2.2 (the disconnect/
    reconnect/credential-failure lifecycle above) and §2.3 (why `.eml`/
    `.mbox` are supported, `.msg` isn't, and a PDF/screenshot of an email
    stays a `Document`) so the privacy/security posture of the whole
    Communications feature is described in one place, current as of this
    step, rather than scattered only across per-step plan entries.

    **Step 11 verification-record correction.** The Step 11 write-up
    above reported its full-suite result from memory rather than from
    the actual last full-suite run; the number quoted there ("1087
    passed, 1 skipped") was actually the *backward-compatibility stash
    baseline* -- the suite run with Step 11's code stashed away, 37
    tests short of the real total. The genuine Step 11 full-suite result
    (with Step 11's code present) was 1124 passed, 1 skipped, confirmed
    against this session's own captured command output and against a
    fresh `pytest --collect-only` count (1125 collected = 1124 + 1
    skipped) at the start of Step 12. No committed documentation had
    repeated the wrong number; this correction lives here for the
    record.
13. *(Gated on Yahoo approving access)* OAuth2 as an added, preferred
    auth option — only if and when approval actually comes through.

Each step: implement → targeted tests → full regression suite → FTS5
guard → scope check → backward-compatibility check → original-file
integrity verification → live smoke test → commit → push → pause for
review.
