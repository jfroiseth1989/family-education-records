"""Small, dependency-free helpers shared across core modules."""

from __future__ import annotations

import re


def slugify(text: str, max_length: int = 40) -> str:
    """Convert arbitrary text into a filesystem/URL-safe slug.

    Used to build human-readable case directory names on disk (e.g.
    ``3-jane-doe-iep-dispute``) from a user-supplied case label, without
    trusting that label to already be safe as a path segment.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:max_length] or "case"
