"""Regression guard: no committed migration drops an FTS5-related table.

See docs/PHASE_3_IMPLEMENTATION_PLAN.md §4 and docs/PHASE_2_FREEZE.md §3:
`alembic revision --autogenerate` has no SQLAlchemy model for
`document_text_fts`/`annotation_notes_fts` or their SQLite-managed shadow
tables (`_data`/`_config`/`_docsize`/`_idx`), so it has misread them as
"extra tables not in the model" and proposed dropping them in every
single migration generated since Step 2 -- five times running as of
Phase 3 Step 0, each caught and hand-trimmed before committing (see each
migration's own docstring). This test codifies "that must always stay
true" instead of relying solely on catching it by eye every time.

This is a static check over the committed migration *files*, not a live
database -- it can't stop a bad autogenerate from being *produced*, but
it fails the suite if one is ever committed without being trimmed first.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations" / "versions"

# Matches op.drop_table('whatever_fts...') / op.drop_table("whatever_fts...")
# -- covers document_text_fts, annotation_notes_fts, and their shadow
# tables (_data/_config/_docsize/_idx), whatever future FTS5 index gets
# added, as long as its table name contains "_fts".
_DROP_FTS_TABLE = re.compile(r"""op\.drop_table\(\s*['"]([^'"]*_fts[^'"]*)['"]""")


def _migration_files() -> list[Path]:
    return sorted(p for p in _MIGRATIONS_DIR.glob("*.py") if p.name != "__init__.py")


def test_at_least_one_migration_exists():
    """Sanity check that this test is actually looking at real files,
    not silently passing over an empty/misconfigured directory.
    """
    assert len(_migration_files()) >= 8


def test_no_migration_drops_an_fts5_table():
    offenders: dict[str, list[str]] = {}
    for path in _migration_files():
        matches = _DROP_FTS_TABLE.findall(path.read_text())
        if matches:
            offenders[path.name] = matches

    assert offenders == {}, (
        "The following migrations drop an FTS5-related table -- almost "
        "certainly an untrimmed autogenerate output that would delete a "
        f"search index: {offenders}"
    )


def test_no_migration_drops_communication_text_fts():
    """Communications Phase Step 6: `communication_text_fts` matches the
    generic `_DROP_FTS_TABLE` pattern above (its name contains "_fts"), so
    it's already covered by `test_no_migration_drops_an_fts5_table` -- this
    test names it explicitly so a future reader doesn't have to rediscover
    that fact, and so a change narrowing the generic pattern would still
    be caught here.
    """
    offenders: list[str] = []
    for path in _migration_files():
        if re.search(r"""op\.drop_table\(\s*['"]communication_text_fts""", path.read_text()):
            offenders.append(path.name)

    assert offenders == [], f"These migrations drop communication_text_fts: {offenders}"
