"""Shared FastAPI dependencies: DB session, vault layout, settings, actor.

Application-wide objects (the SQLAlchemy session factory, the resolved
vault layout, settings) are attached to ``app.state`` once at startup by
``app/main.py::create_app`` and read back out here via ``Request.app.state``
— this keeps route handlers free of import-time global state, which makes
them straightforward to exercise in tests against a temporary vault.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.vault import VaultLayout


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def get_vault(request: Request) -> VaultLayout:
    return request.app.state.vault


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_actor(request: Request) -> str:
    return request.app.state.settings.resolved_actor_name()
