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
