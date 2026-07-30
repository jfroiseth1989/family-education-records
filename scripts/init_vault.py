#!/usr/bin/env python3
"""Initialize a new (or reconnect to an existing) vault directory.

Usage:
    .venv/bin/python scripts/init_vault.py [--path /custom/vault/location]

Run this once before first starting the app, or any time you want to set
up a vault at a non-default location. Safe to re-run against an existing
vault — it never overwrites vault.json or touches any case data, and the
underlying init_vault()/run_migrations() calls are the same idempotent
functions app/main.py runs on every startup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.vault import VaultInsideGitRepoError, init_vault  # noqa: E402
from app.db.migrate import run_migrations  # noqa: E402
from app.db.seed import seed_document_types  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path.home() / "FERPA-Evidence-Vault",
        help="Vault directory to create or reconnect to (default: ~/FERPA-Evidence-Vault)",
    )
    args = parser.parse_args()

    try:
        vault = init_vault(args.path)
    except VaultInsideGitRepoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    run_migrations(vault.db_path)

    engine = make_engine(vault.db_path)
    session_factory = make_session_factory(engine)
    with session_factory() as db:
        seed_document_types(db)

    print(f"Vault ready at: {vault.root}")
    print(f"  Database:  {vault.db_path}")
    print(f"  Cases dir: {vault.cases_dir}")
    print()
    print("If this isn't the default location, set FERPA_VAULT_PATH before")
    print("starting the app (e.g. in a .env file in the repo root):")
    print(f"  FERPA_VAULT_PATH={vault.root}")


if __name__ == "__main__":
    main()
