"""Tests for the vault lifecycle and the git-repo guardrail.

The guardrail (docs/PRIVACY_SECURITY.md §1) is the single control this
whole application design leans on to guarantee case data can never end up
inside a git-tracked directory. It must actually refuse to run, not just
warn.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.core.vault import (
    VaultInsideGitRepoError,
    assert_vault_not_in_git_repo,
    find_enclosing_git_root,
    init_vault,
)


def test_find_enclosing_git_root_none_outside_any_repo(tmp_path: Path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    assert find_enclosing_git_root(plain_dir) is None


def test_find_enclosing_git_root_detects_ancestor_repo(tmp_path: Path):
    repo_root = tmp_path / "some-repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    nested = repo_root / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert find_enclosing_git_root(nested) == repo_root


def test_find_enclosing_git_root_handles_worktree_gitfile(tmp_path: Path):
    # In a git worktree/submodule, `.git` is a *file* pointing elsewhere,
    # not a directory -- the guardrail must still catch this case.
    repo_root = tmp_path / "worktree-repo"
    repo_root.mkdir()
    (repo_root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

    assert find_enclosing_git_root(repo_root) == repo_root


def test_assert_vault_not_in_git_repo_raises_inside_repo(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    vault_candidate = repo_root / "oops-vault"

    with pytest.raises(VaultInsideGitRepoError):
        assert_vault_not_in_git_repo(vault_candidate)


def test_assert_vault_not_in_git_repo_allows_outside_repo(tmp_path: Path):
    vault_candidate = tmp_path / "fine-vault"
    # Should not raise.
    assert_vault_not_in_git_repo(vault_candidate)


def test_init_vault_refuses_path_inside_real_git_repo(tmp_path: Path):
    """End-to-end check using an actual `git init`, not just a `.git` stand-in."""
    repo_root = tmp_path / "real-repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)

    with pytest.raises(VaultInsideGitRepoError):
        init_vault(repo_root / "vault")


def test_init_vault_creates_expected_layout(tmp_path: Path):
    vault_path = tmp_path / "vault"
    layout = init_vault(vault_path)

    assert layout.root == vault_path
    assert layout.cases_dir.is_dir()
    assert layout.metadata_path.is_file()

    metadata = json.loads(layout.metadata_path.read_text())
    assert metadata["format_version"] == 1
    assert "created_at" in metadata


def test_init_vault_is_idempotent(tmp_path: Path):
    vault_path = tmp_path / "vault"
    layout1 = init_vault(vault_path)
    original_metadata = layout1.metadata_path.read_text()

    layout2 = init_vault(vault_path)

    assert layout2.metadata_path.read_text() == original_metadata
