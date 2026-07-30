"""Tests for the low-level file helpers: hashing, read-only, copy-in."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from app.core.files import compute_sha256, copy_into_vault, make_read_only

# The write-permission bits `make_read_only` clears.
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH

# The root user ignores file permission bits on POSIX systems, so a real
# write attempt would succeed even after make_read_only() -- that's an
# environment property, not a bug in the function. We still assert the
# mode bits are cleared (the portable, always-true part of the contract)
# and only assert the enforced-write-fails behavior when not root.
_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_compute_sha256_matches_hashlib_reference(tmp_path: Path):
    path = tmp_path / "sample.txt"
    content = b"some document content" * 1000
    path.write_bytes(content)

    assert compute_sha256(path) == hashlib.sha256(content).hexdigest()


def test_copy_into_vault_does_not_modify_source(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("original content")
    original_hash = compute_sha256(source)

    destination = tmp_path / "vault" / "abc123" / "source.txt"
    copy_into_vault(source, destination)

    assert destination.read_text() == "original content"
    assert compute_sha256(source) == original_hash
    assert source.exists()  # copy, not move


def test_make_read_only_clears_write_permission_bits(tmp_path: Path):
    path = tmp_path / "protected.txt"
    path.write_text("do not change me")

    make_read_only(path)

    assert path.stat().st_mode & _WRITE_BITS == 0


@pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="root bypasses file permission bits on POSIX"
)
def test_make_read_only_prevents_writes_for_non_root_user(tmp_path: Path):
    path = tmp_path / "protected.txt"
    path.write_text("do not change me")

    make_read_only(path)

    with pytest.raises(PermissionError):
        path.write_text("attempted overwrite")

    assert path.read_text() == "do not change me"
