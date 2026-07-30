"""Application configuration.

Settings are sourced from environment variables (prefix ``FERPA_``) or a
``.env`` file in the working directory — never hardcoded paths or secrets.
The vault path is the single most privacy-sensitive setting in this file;
see docs/PRIVACY_SECURITY.md §1 for why it must default outside the repo.
"""

from __future__ import annotations

import getpass
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable via ``FERPA_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="FERPA_", env_file=".env", extra="ignore")

    # Default vault location is outside this repository on purpose — see
    # docs/PRIVACY_SECURITY.md §1. Override with FERPA_VAULT_PATH.
    vault_path: Path = Path.home() / "FERPA-Evidence-Vault"

    # Loopback-only by default — see docs/PRIVACY_SECURITY.md §2. Do not
    # change this default to "0.0.0.0"; if remote access is ever needed,
    # that's a deliberate, documented decision, not a config default.
    host: str = "127.0.0.1"
    port: int = 8420

    # Empty string means "resolve from the OS at runtime" — see
    # resolved_actor_name(). Override with FERPA_ACTOR_NAME to set a fixed
    # display name for chain-of-custody / audit log entries.
    actor_name: str = ""

    def resolved_actor_name(self) -> str:
        """Return the configured actor name, or fall back to the OS username."""
        return self.actor_name or _default_actor_name()


def _default_actor_name() -> str:
    try:
        return getpass.getuser()
    except Exception:
        # getpass.getuser() can fail in some sandboxed/containerized
        # environments with no user database entry; fall back rather than
        # crash startup over a cosmetic label.
        return "local-user"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton (cached after first call)."""
    return Settings()
