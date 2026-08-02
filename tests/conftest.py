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
from app.db.seed import seed_annotation_types, seed_document_types, seed_event_types, seed_fact_types
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
        seed_event_types(db)
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
def anonymous_client(app) -> TestClient:
    """A TestClient with no account set up and no session -- the state a
    real first-time visitor (or Security Phase Step 3's enforcement
    middleware, for any request outside /auth/* and /static/*) sees.

    Named `anonymous_client` (rather than `client`) so it's opt-in: tests
    that specifically exercise pre-setup/pre-login/logged-out behavior ask
    for this fixture by name, while the default `client` fixture below is
    pre-authenticated so the ~550 feature tests written before Step 3's
    enforcement middleware existed don't all need to perform their own
    login dance.
    """
    return TestClient(app)


TEST_OWNER_PASSWORD = "owner-password-123"


@pytest.fixture
def client(anonymous_client: TestClient) -> TestClient:
    """A TestClient that has already completed first-run setup (which also
    logs it in) via a real HTTP round-trip -- not a bypass. This is what
    lets Step 3's deny-by-default enforcement middleware actually run in
    front of every feature test while leaving those tests themselves
    unchanged: they get a client that legitimately holds a valid session
    cookie, the same way a real browser would after visiting /auth/setup.
    """
    csrf_response = anonymous_client.get("/auth/setup")
    csrf_token = anonymous_client.cookies.get("csrf_token") or csrf_response.cookies.get("csrf_token")
    setup_response = anonymous_client.post(
        "/auth/setup",
        data={
            "password": TEST_OWNER_PASSWORD,
            "confirm_password": TEST_OWNER_PASSWORD,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert setup_response.status_code == 303, setup_response.text
    return anonymous_client
