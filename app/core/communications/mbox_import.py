"""Manual `.mbox` import: splitting an mbox archive into its individual
real messages and feeding each one through the existing `.eml` ingestion
pipeline unchanged (Communications Phase Step 8).

Per docs/COMMUNICATIONS_PLAN.md Step 8's investigation, `.mbox` is the
one additional saved-email format that clears the evidence/provenance
bar cleanly enough to implement this step (unlike `.msg`, `.mbx`,
`.oft`, and `.emlx` -- see that section for the full reasoning). An
mbox file is just a concatenation of real RFC822 messages separated by
"From " lines; splitting it does not transform, normalize, or
reconstruct anything about a message's own content -- each extracted
message's bytes are that message's own genuine original, exactly as if
it had been saved individually as a `.eml` file. That is what makes it
safe to reuse `import_eml_file()` verbatim rather than writing a
second, parallel parser: the *file offered to FERChronos* was an
archive, which is why `import_method="mbox_import"` and
`custody_details` record that distinctly, but the *message* is not
materially different from any other RFC822 original.

Uses only Python's stdlib `mailbox` module -- no new dependency, no
native/system requirement, so this carries none of the licensing or
packaging risk that ruled out `.msg` support this step.
"""

from __future__ import annotations

import mailbox
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.communications.ingestion import DuplicateCommunicationError, import_eml_file
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import Case, Communication


@dataclass
class MboxImportResult:
    """Summary of one mbox archive import -- an mbox can contain many
    messages, so there is no single resulting `Communication` to
    redirect to; the API route surfaces this summary instead."""

    imported: list[Communication] = field(default_factory=list)
    duplicate_count: int = 0
    unparseable_count: int = 0

    @property
    def total_messages(self) -> int:
        return len(self.imported) + self.duplicate_count + self.unparseable_count


def import_mbox_file(
    db: Session,
    vault: VaultLayout,
    case: Case,
    source_file_path: Path,
    original_filename: str,
    actor: str,
) -> MboxImportResult:
    """Split `source_file_path` (an mbox archive) into its individual
    messages and import each one via `import_eml_file()`.

    Never modifies or deletes `source_file_path` -- `mailbox.mbox` opens
    it for reading only, and each entry's raw bytes are copied out to a
    throwaway temp file before being handed to `import_eml_file()`,
    which does its own independent copy into the vault. A duplicate
    message (already imported previously, or repeated within the same
    archive) is skipped, not treated as a batch-aborting error, so the
    same archive can safely be re-imported after new messages are added
    to it. Does not commit; the caller controls the transaction
    boundary, and each successfully imported message is flushed (not
    committed) as it goes, matching `import_eml_file()`'s own contract.
    """
    archive_hash = compute_sha256(source_file_path)
    custody_details = {
        "source_archive_filename": original_filename,
        "source_archive_sha256": archive_hash,
    }

    result = MboxImportResult()
    mbox = mailbox.mbox(str(source_file_path), factory=None, create=False)
    try:
        for key in mbox.keys():
            raw_bytes = mbox.get_bytes(key)
            if not raw_bytes.strip():
                result.unparseable_count += 1
                continue

            with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp:
                tmp.write(raw_bytes)
                tmp_path = Path(tmp.name)
            try:
                message_filename = f"{original_filename}-message-{key + 1}.eml"
                try:
                    communication = import_eml_file(
                        db,
                        vault,
                        case,
                        source_file_path=tmp_path,
                        original_filename=message_filename,
                        actor=actor,
                        import_method="mbox_import",
                        custody_details=custody_details,
                    )
                except DuplicateCommunicationError:
                    result.duplicate_count += 1
                else:
                    result.imported.append(communication)
            finally:
                tmp_path.unlink(missing_ok=True)
    finally:
        mbox.close()

    return result
