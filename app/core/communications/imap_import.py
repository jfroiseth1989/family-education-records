"""Import one mailbox message, by `(folder, uid)`, from an already-
connected `ImapClient` into the shared Communications ingestion pipeline
(Communications Phase Step 10).

This is the one place raw bytes cross from "still sitting on Yahoo's
server" to "handed to `import_eml_file()`" -- it does no parsing,
storage, deduplication, threading, classification, or timeline-
suggestion logic of its own; all of that is the shared pipeline's job,
unchanged. Mirrors app/core/communications/mbox_import.py's shape
exactly (fetch/extract raw bytes -> temp file -> `import_eml_file()`),
the same pattern Step 8 established for turning "bytes from somewhere
else" into a `Communication` without a second parser.

Uses `ImapClient.fetch_raw_message()` -- read-only by construction (see
imap_client.py's module docstring): a fresh, read-only `SELECT` and a
`BODY.PEEK[]` fetch, so importing a message never marks it `\\Seen`,
moves it, copies it, or otherwise changes anything on Yahoo's server.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.communications.imap_client import ImapClient
from app.core.communications.ingestion import import_eml_file
from app.core.vault import VaultLayout
from app.db.models import Case, Communication

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_component(value: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("_", value).strip("_") or "folder"


def import_one_imap_message(
    db: Session,
    vault: VaultLayout,
    case: Case,
    imap_client: ImapClient,
    account_id: int,
    folder: str,
    uid: str,
    actor: str,
) -> Communication:
    """Fetch `(folder, uid)`'s raw RFC822 bytes and import it via
    `import_eml_file()`, recording `account_id`/`mailbox_folder`/
    `mailbox_uid` for provenance and account-scoped duplicate detection.

    Raises whatever `ImapClient.fetch_raw_message()` raises (a fetch
    failure) or `DuplicateCommunicationError` (an already-imported
    message) -- both left for the caller (the import batch worker) to
    handle per-item, exactly as with `mbox_import.py`. Does not commit;
    the caller controls the transaction boundary, same as
    `import_eml_file()` itself.
    """
    raw_bytes = imap_client.fetch_raw_message(folder, uid)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp:
        tmp.write(raw_bytes)
        tmp_path = Path(tmp.name)
    try:
        original_filename = f"yahoo-{_safe_filename_component(folder)}-{uid}.eml"
        return import_eml_file(
            db,
            vault,
            case,
            source_file_path=tmp_path,
            original_filename=original_filename,
            actor=actor,
            import_method="imap_sync",
            custody_details={"source": "yahoo_imap", "mailbox_folder": folder, "mailbox_uid": uid},
            account_id=account_id,
            mailbox_folder=folder,
            mailbox_uid=uid,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
