"""FastAPI application entrypoint.

Binds only to ``127.0.0.1`` by default — see docs/PRIVACY_SECURITY.md §2.
``Settings.host`` controls this; do not change the default to ``0.0.0.0``.

Startup sequence: resolve settings -> initialize/guard the vault -> run
database migrations to head -> seed default lookup data -> sweep any
OCR jobs left stuck ``running`` by a prior crash -> start the background
OCR worker (if enabled) -> mount routers. No step here ever falls back to
``Base.metadata.create_all`` — every schema change goes through Alembic
(see app/db/migrate.py), including the very first one, so there is
exactly one way the database schema comes into existence.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.api import (
    account,
    annotations,
    auth,
    cases,
    communications,
    documents,
    facts,
    ocr,
    search,
    tags,
    timeline,
)
from app.config import Settings, get_settings
from app.core.auth.enforcement import AuthEnforcementMiddleware
from app.core.auth.session import get_current_session
from app.core.vault import VaultLayout, init_vault
from app.db.migrate import run_migrations
from app.db.models import AppSession, Case
from app.db.seed import seed_annotation_types, seed_document_types, seed_event_types, seed_fact_types
from app.db.session import make_engine, make_session_factory
from app.jobs.worker import run_worker_loop, sweep_stuck_jobs

BASE_DIR = Path(__file__).resolve().parent


def _list_students_for_selector(request: Request) -> list[Case]:
    """Jinja global backing the persistent student selector in base.html.

    A global function (not per-route context) so the header's selector
    works on every page without every route handler needing to pass the
    full student list through its own template context. Opens and closes
    its own short-lived session -- fine at this app's single-user, small-
    student-count scale (see docs/PRIVACY_SECURITY.md for the no-
    network/local-only model this app already assumes).
    """
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        return list(db.scalars(select(Case).order_by(Case.label)).all())


def _header_session(request: Request) -> AppSession | None:
    """Jinja global backing base.html's Log in / Lock+Log out display
    (Security Phase Step 2).

    Purely informational in this step -- see app/core/auth/session.py's
    module docstring. Nothing here blocks access; a later, separate step
    adds the actual enforcement middleware. Same short-lived-session
    pattern as `_list_students_for_selector` above.
    """
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        return get_current_session(request, db)


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
        seed_annotation_types(db)
        seed_fact_types(db)
        seed_event_types(db)

    with session_factory() as db:
        # Any OCR job still "running" at this point was left that way by
        # an interrupted worker/app process, not a job actually in
        # progress right now (nothing has started the worker yet on this
        # run) — see app/jobs/worker.py::sweep_stuck_jobs.
        sweep_stuck_jobs(db)

    app = FastAPI(title="FERChronos", version="0.1.0")
    app.state.vault = vault
    app.state.session_factory = session_factory
    app.state.settings = settings

    app.add_middleware(AuthEnforcementMiddleware)

    if settings.enable_background_worker:
        # A single background worker thread, started once per app
        # instance — see app/jobs/worker.py. `daemon=True` is deliberate,
        # not a default left unconsidered: `run_worker_loop` only returns
        # once `stop_event` is set, which nothing currently does at
        # shutdown, so a *non*-daemon thread here would hang process exit
        # indefinitely (this was checked, not assumed — a
        # ThreadPoolExecutor's worker threads are joined at interpreter
        # exit and would have exactly this problem). A daemon thread is
        # simply killed on process exit instead, which is the correct
        # behavior for a single-user, single-machine tool with no
        # in-flight-job durability guarantee beyond "resume via the
        # startup recovery sweep next time the app starts" (see
        # sweep_stuck_jobs above). `stop_event` is still stored on
        # app.state so a future graceful-shutdown hook can use it.
        stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=run_worker_loop, args=(session_factory, vault, stop_event), daemon=True
        )
        worker_thread.start()
        app.state.ocr_worker_thread = worker_thread
        app.state.ocr_worker_stop_event = stop_event

    app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")
    app.state.templates.env.globals["all_students"] = _list_students_for_selector
    app.state.templates.env.globals["header_session"] = _header_session

    app.include_router(account.router)
    app.include_router(annotations.router)
    app.include_router(auth.router)
    app.include_router(cases.router)
    app.include_router(communications.router)
    app.include_router(documents.router)
    app.include_router(facts.router)
    app.include_router(ocr.router)
    app.include_router(search.router)
    app.include_router(tags.router)
    app.include_router(timeline.router)

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
