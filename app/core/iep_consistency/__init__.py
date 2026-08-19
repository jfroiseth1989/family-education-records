"""IEP Consistency Review Step 3: deterministic comparison and the
inconsistency-flag review lifecycle.

See docs/IEP_CONSISTENCY_REVIEW_PLAN.md sections 4 and 7. This package
is deliberately separate from app/core/iep_extraction/ (which only
produces `iep_records`/`iep_record_fields`) -- extraction and
comparison are two distinct layers, matching the same separation the
plan requires between deterministic comparison here and any future,
structurally separate semantic/AI-assisted suggestion layer (not built
in this step, and not read by anything here).

- `rules.py`: pure, deterministic comparison functions. No database
  writes -- given already-loaded `IepRecord` rows, returns candidate
  mismatches as plain data (`FlagCandidate`).
- `service.py`: turns those candidates into `iep_inconsistency_flags`
  rows idempotently (via `dedup_key`), and implements the
  pending/confirmed/dismissed review-lifecycle transitions.

Step 3 scope is deliberately narrow: within-document comparison of
`service`-type `iep_records` only -- the one structured type
app/core/iep_extraction/ builds as of Step 2. Across-document
comparison (via confirmed `iep_document_links`) and other record types
(goals, accommodations, dates, eligibility) are later, separately
approved steps (see the plan's §12 staged sequence).
"""
