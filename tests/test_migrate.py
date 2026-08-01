"""Tests for app/db/migrate.py -- the sole path by which the schema is created."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrate import run_migrations

EXPECTED_TABLES = {
    "cases",
    "document_types",
    "document_version_groups",
    "documents",
    "document_custody_events",
    "audit_log",
    "document_pages",
    "citations",
    "alembic_version",
}


def test_run_migrations_creates_all_expected_tables(tmp_path: Path):
    db_path = tmp_path / "vault" / "db.sqlite"
    db_path.parent.mkdir(parents=True)

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    assert EXPECTED_TABLES.issubset(tables)


def test_run_migrations_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "vault" / "db.sqlite"
    db_path.parent.mkdir(parents=True)

    run_migrations(db_path)
    run_migrations(db_path)  # must not raise or duplicate anything

    conn = sqlite3.connect(db_path)
    try:
        version_rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()

    assert len(version_rows) == 1


def test_migrations_apply_cleanly_against_a_populated_documents_table(tmp_path: Path):
    """Regression test: the Step 1 migration adds NOT NULL columns
    (needs_ocr, extraction_status) to `documents` via ALTER TABLE. SQLite
    refuses to add a NOT NULL column with no default to a non-empty table
    -- this was an actual bug caught during development (the first draft
    of the migration had no server_default and failed exactly this way).
    Runs the full migration chain against a database that already has a
    real document row, simulating an existing vault being upgraded.
    """
    db_path = tmp_path / "vault" / "db.sqlite"
    db_path.parent.mkdir(parents=True)

    # Bring the DB to the revision immediately before the extraction-schema
    # migration, insert a row, then upgrade the rest of the way to head.
    from app.db.migrate import _make_alembic_config
    from alembic import command

    config = _make_alembic_config(db_path)
    command.upgrade(config, "79c226771ac3")  # last revision before extraction schema

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO cases (case_id, label, status) VALUES (1, 'Test', 'active')")
    conn.execute(
        """
        INSERT INTO documents
            (document_id, case_id, original_filename, stored_path, sha256_hash,
             file_size_bytes, ingested_by, is_current_version)
        VALUES (1, 1, 'x.txt', 'cases/1/originals/x/x.txt', 'deadbeef', 10, 'test-user', 1)
        """
    )
    conn.commit()
    conn.close()

    command.upgrade(config, "head")  # must not raise

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT needs_ocr, extraction_status FROM documents WHERE document_id=1"
        ).fetchone()
    finally:
        conn.close()

    assert row == (0, "pending")
