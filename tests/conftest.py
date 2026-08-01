"""Shared pytest fixtures.

Every test gets its own throwaway vault directory under pytest's `tmp_path`,
so tests never touch a real vault, never risk colliding with each other,
and never depend on any pre-existing machine state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.vault import VaultLayout, init_vault
from app.db.migrate import run_migrations
from app.db.models import Case
from app.db.seed import seed_annotation_types, seed_document_types, seed_fact_types
from app.db.session import make_engine, make_session_factory
from app.main import create_app


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def settings(vault_path: Path) -> Settings:
    # enable_background_worker=False: tests exercise the OCR job queue's
    # processing logic directly and synchronously (app/jobs/worker.py's
    # process_next_job) rather than via a real background thread, so the
    # 240+ tests using this fixture don't each spin up one against a
    # throwaway vault. See app/config.py's Settings.enable_background_worker.
    return Settings(vault_path=vault_path, actor_name="test-user", enable_background_worker=False)


@pytest.fixture
def vault(vault_path: Path) -> VaultLayout:
    return init_vault(vault_path)


@pytest.fixture
def db_session(vault: VaultLayout):
    """A DB session against a freshly migrated + seeded vault database."""
    run_migrations(vault.db_path)
    engine = make_engine(vault.db_path)
    session_factory = make_session_factory(engine)
    with session_factory() as db:
        seed_document_types(db)
        seed_annotation_types(db)
        seed_fact_types(db)
        yield db


@pytest.fixture
def sample_case(db_session: Session) -> Case:
    case = Case(label="Jane Doe — Test Case", description="Fixture case for tests.")
    db_session.add(case)
    db_session.commit()
    return case


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """A file outside the vault, standing in for a document to be ingested.

    Tests can assert this file is untouched after ingestion to verify the
    "originals are never modified" guarantee.
    """
    path = tmp_path / "source-files" / "iep-2024.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("This is the original IEP document text.\n")
    return path


@pytest.fixture
def app(settings: Settings):
    return create_app(settings=settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
