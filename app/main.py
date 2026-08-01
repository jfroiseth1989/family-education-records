"""FastAPI application entrypoint.

Binds only to ``127.0.0.1`` by default — see docs/PRIVACY_SECURITY.md §2.
``Settings.host`` controls this; do not change the default to ``0.0.0.0``.

Startup sequence: resolve settings -> initialize/guard the vault -> run
database migrations to head -> seed default lookup data -> mount routers.
No step here ever falls back to ``Base.metadata.create_all`` — every schema
change goes through Alembic (see app/db/migrate.py), including the very
first one, so there is exactly one way the database schema comes into
existence.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import cases, documents, search, tags
from app.config import Settings, get_settings
from app.core.vault import VaultLayout, init_vault
from app.db.migrate import run_migrations
from app.db.seed import seed_document_types
from app.db.session import make_engine, make_session_factory

BASE_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app, wired to a vault resolved from ``settings``.

    Accepts an explicit ``settings`` argument (rather than only reading the
    process-wide singleton) so tests can point the app at an isolated,
    temporary vault without touching environment variables.
    """
    settings = settings or get_settings()
    vault: VaultLayout = init_vault(settings.vault_path)

    run_migrations(vault.db_path)

    engine = make_engine(vault.db_path)
    session_factory = make_session_factory(engine)

    with session_factory() as db:
        seed_document_types(db)

    app = FastAPI(title="FERPA Evidence Manager", version="0.1.0")
    app.state.vault = vault
    app.state.session_factory = session_factory
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

    app.include_router(cases.router)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(tags.router)

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/cases")

    return app


# Deliberately no module-level `app = create_app()` here: create_app()
# initializes the vault (creating directories, running migrations) as a
# side effect, and this module must be importable — e.g. by tests wanting
# `create_app` — without that side effect firing against a real, unrelated
# vault. Run the server with uvicorn's application-factory mode instead:
#     uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8420
# which is exactly what scripts/start.sh / start.bat do.
