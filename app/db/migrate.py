"""Programmatic Alembic entrypoint: bring a vault's database up to date.

The application never issues raw `CREATE TABLE` / `Base.metadata.create_all`
against a live vault — every schema change, including the very first one,
goes through an Alembic revision, so upgrading between versions of this
application is always an explicit, reviewable migration rather than an
implicit reconciliation. See app/db/migrations/README.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _make_alembic_config(db_path: Path) -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_REPO_ROOT / "app" / "db" / "migrations"))
    config.attributes["sqlalchemy_url"] = f"sqlite:///{db_path}"
    return config


def run_migrations(db_path: Path) -> None:
    """Upgrade the database at ``db_path`` to the latest revision (head).

    Idempotent — running against an already up-to-date database is a no-op.
    Creates the database file (via SQLite's implicit file creation) if it
    doesn't exist yet.
    """
    config = _make_alembic_config(db_path)
    command.upgrade(config, "head")
