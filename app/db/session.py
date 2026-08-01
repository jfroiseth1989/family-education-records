"""Database engine and session factory.

One SQLite database per vault (``db.sqlite``), shared across all cases —
see docs/ARCHITECTURE.md §5. Every table carries a ``case_id`` so this
single-file design keeps cross-case queries and backups simple.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_path: Path) -> Engine:
    """Create a SQLite engine at ``db_path`` with foreign keys enforced.

    SQLite does not enforce foreign key constraints unless explicitly told
    to on every connection — without this, the integrity guarantees
    designed into the schema (e.g. a custody event always referencing a
    real document) would be silently unenforced.

    Also enables WAL journal mode and a busy timeout — required starting
    Phase 3 Step 0, which introduces this app's first background thread
    (the OCR job worker, app/jobs/worker.py) reading/writing the same
    SQLite file concurrently with request-handling threads. Every prior
    phase was single-threaded against the database, so this condition
    never existed before. WAL lets readers proceed while a writer is
    active instead of blocking; the busy timeout makes any residual lock
    contention retry-wait briefly rather than immediately raising
    "database is locked".
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
