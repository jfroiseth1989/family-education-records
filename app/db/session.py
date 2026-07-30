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
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
