"""Vault lifecycle: locating, guarding, and initializing the local data vault.

The vault is the directory that holds all case data — original files,
the database, and (in later phases) extracted text, OCR output, and
exports. It is deliberately kept outside this git repository so that case
data can never be accidentally committed or pushed — see
docs/PRIVACY_SECURITY.md §1 and docs/ARCHITECTURE.md §5.

This module owns the one hard guardrail that backs that promise: refusing
to initialize or use a vault path that lives inside a git working tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.utils import slugify

VAULT_METADATA_FILENAME = "vault.json"
VAULT_FORMAT_VERSION = 1


class VaultInsideGitRepoError(RuntimeError):
    """Raised when the configured vault path is inside a git working tree."""


def find_enclosing_git_root(path: Path) -> Path | None:
    """Return the git working-tree root that contains ``path``, if any.

    Walks upward from ``path`` looking for a ``.git`` entry — a directory
    for an ordinary clone, or a file for a worktree/submodule (git writes a
    ``gitdir: ...`` pointer file in those cases). Returns ``None`` if no
    ancestor directory has one.

    This is a plain filesystem check with no dependency on the ``git``
    binary being installed, so the guardrail works even in minimal
    environments.
    """
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def assert_vault_not_in_git_repo(vault_path: Path) -> None:
    """Refuse to proceed if ``vault_path`` is inside a git working tree.

    This is a hard failure, not a warning — it runs before anything is
    written to the vault path, both at explicit vault initialization and
    at every application startup (see :func:`init_vault`).
    """
    repo_root = find_enclosing_git_root(vault_path)
    if repo_root is not None:
        raise VaultInsideGitRepoError(
            f"Refusing to use vault path '{vault_path}' because it is inside "
            f"a git working tree rooted at '{repo_root}'. Case data must "
            "never be stored inside a git repository, so it can never be "
            "accidentally committed or pushed. Choose a vault path outside "
            "any git-tracked directory (e.g. the default, "
            "~/FERPA-Evidence-Vault)."
        )


@dataclass(frozen=True)
class VaultLayout:
    """Resolved filesystem paths for a single vault."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "db.sqlite"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def metadata_path(self) -> Path:
        return self.root / VAULT_METADATA_FILENAME

    def case_slug_dir_name(self, case_id: int, label: str) -> str:
        return f"{case_id}-{slugify(label)}"

    def case_dir(self, case_id: int, label: str) -> Path:
        return self.cases_dir / self.case_slug_dir_name(case_id, label)

    def originals_dir(self, case_id: int, label: str) -> Path:
        return self.case_dir(case_id, label) / "originals"


def init_vault(vault_path: Path) -> VaultLayout:
    """Create (or reconnect to) a vault at ``vault_path``.

    Idempotent — safe to call every time the application starts, whether
    the vault already exists or not. Always re-checks the git guardrail
    first, since the configured path could change between runs.
    """
    assert_vault_not_in_git_repo(vault_path)

    vault_path.mkdir(parents=True, exist_ok=True)
    layout = VaultLayout(root=vault_path)
    layout.cases_dir.mkdir(parents=True, exist_ok=True)

    if not layout.metadata_path.exists():
        metadata = {
            "format_version": VAULT_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        layout.metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

    return layout
