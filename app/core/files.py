"""Filesystem helpers for handling original document files.

These are the only functions in the application permitted to write into a
document's storage location under ``originals/``, and only at the moment of
ingestion. After a file is copied in and made read-only, no other code path
in this application opens it in write mode — see docs/PRIVACY_SECURITY.md §3.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

_HASH_CHUNK_SIZE = 1024 * 1024  # 1 MiB — bounds memory use for large files


def compute_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file, streaming to bound memory use."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_read_only(path: Path) -> None:
    """Set a file read-only at the OS level, on both POSIX and Windows.

    This is a best-effort integrity signal, not a security boundary —
    anyone with OS-level access to the account could reverse it. Its real
    value is making accidental modification harder, and detectable via the
    SHA-256 hash recorded at ingestion (see docs/PRIVACY_SECURITY.md §3).
    """
    current_mode = path.stat().st_mode
    read_only_mode = current_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    os.chmod(path, read_only_mode)


def copy_into_vault(source_path: Path, destination_path: Path) -> None:
    """Copy ``source_path`` into the vault at ``destination_path``.

    Never moves, links, or otherwise modifies ``source_path`` — it is only
    ever opened for reading. Uses ``copy2`` to preserve the source's
    timestamps/metadata on the copy, for whatever provenance value that
    carries, but that has no bearing on the source file's own integrity.
    """
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
