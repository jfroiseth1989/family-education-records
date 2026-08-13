"""Chain-of-custody ledger writes for communications (Communications
Phase Step 3).

Mirrors app/core/custody.py exactly, for `Communication` rows instead of
`Document` rows -- see that module's docstring and
docs/COMMUNICATIONS_PLAN.md §2/§4. Nothing in this application exposes
an update or delete path for `communication_custody_events`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Communication, CommunicationCustodyEvent


def write_communication_custody_event(
    db: Session,
    communication: Communication,
    event_type: str,
    actor: str,
    details: dict | None = None,
) -> CommunicationCustodyEvent:
    """Append a custody event for `communication`.

    Snapshots the communication's *current* hash/size/location onto the
    event, same convention as `write_custody_event()` for documents.
    Does not commit; the caller controls the transaction boundary.
    """
    event = CommunicationCustodyEvent(
        communication=communication,
        event_type=event_type,
        actor=actor,
        sha256_hash_at_event=communication.sha256_hash,
        file_size_bytes_at_event=communication.file_size_bytes,
        storage_location_at_event=communication.stored_path,
        details=details,
    )
    db.add(event)
    return event
