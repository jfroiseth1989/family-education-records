"""Service-level tests for thread reconstruction as wired into
`import_eml_file()` (Communications Phase Step 4): DB reconciliation,
idempotence, out-of-order/reply-before-parent handling, and that raw
email provenance is untouched by threading.

Pure grouping-algorithm tests live in tests/test_communications_threading.py.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.communications.ingestion import import_eml_file
from app.core.communications.thread_rebuild import rebuild_threads
from app.core.files import compute_sha256
from app.core.vault import VaultLayout
from app.db.models import Case, Communication, CommunicationThread


def _eml(
    *,
    message_id: str,
    subject: str = "IEP Meeting",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    date: str = "Mon, 7 Mar 2022 14:30:00 -0500",
    from_addr: str = "teacher@district.example.org",
    to_addr: str = "parent@yahoo.com",
    body: str = "See above.",
) -> bytes:
    lines = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {' '.join(references)}")
    return ("\n".join(lines) + f"\n\n{body}\n").encode("utf-8")


def _write(tmp_path: Path, raw: bytes, name: str) -> Path:
    path = tmp_path / "source-files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _import(db: Session, vault: VaultLayout, case: Case, tmp_path: Path, raw: bytes, name: str) -> Communication:
    source = _write(tmp_path, raw, name)
    communication = import_eml_file(
        db, vault, case, source_file_path=source, original_filename=name, actor="test-user"
    )
    db.commit()
    return communication


# --- basic thread creation ------------------------------------------------


def test_reply_chain_creates_one_thread_with_correct_metadata(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    parent = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<p@x>", subject="IEP Meeting", date="Mon, 7 Mar 2022 09:00:00 -0500"),
        "parent.eml",
    )
    reply = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(
            message_id="<r@x>",
            subject="Re: IEP Meeting",
            in_reply_to="<p@x>",
            date="Tue, 8 Mar 2022 09:00:00 -0500",
            from_addr="parent@yahoo.com",
            to_addr="teacher@district.example.org",
        ),
        "reply.eml",
    )

    db_session.refresh(parent)
    db_session.refresh(reply)
    assert parent.thread_id is not None
    assert parent.thread_id == reply.thread_id

    thread = db_session.get(CommunicationThread, parent.thread_id)
    assert thread.message_count == 2
    assert thread.subject_normalized == "IEP Meeting"
    assert thread.case_id == sample_case.case_id
    assert "parent@yahoo.com" in thread.participant_summary
    assert "teacher@district.example.org" in thread.participant_summary


def test_single_unthreaded_message_gets_no_thread(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    lone = _import(
        db_session, vault, sample_case, tmp_path, _eml(message_id="<solo@x>"), "solo.eml"
    )
    db_session.refresh(lone)
    assert lone.thread_id is None
    assert db_session.query(CommunicationThread).count() == 0


# --- reply imported before its parent -------------------------------------


def test_reply_imported_before_parent_is_reconciled_once_parent_arrives(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    reply = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<r@x>", in_reply_to="<p@x>", subject="Re: IEP Meeting"),
        "reply-first.eml",
    )
    db_session.refresh(reply)
    assert reply.thread_id is None  # parent not seen yet -- singleton, no thread

    parent = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<p@x>", subject="IEP Meeting"),
        "parent-second.eml",
    )
    db_session.refresh(reply)
    db_session.refresh(parent)

    assert parent.thread_id is not None
    assert parent.thread_id == reply.thread_id
    thread = db_session.get(CommunicationThread, parent.thread_id)
    assert thread.message_count == 2


# --- out-of-order imports ---------------------------------------------------


def test_out_of_order_dates_still_group_correctly(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    # Import the *later* message first, then the earlier one.
    later = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(
            message_id="<later@x>", in_reply_to="<earlier@x>",
            subject="Re: Field Trip", date="Wed, 9 Mar 2022 09:00:00 -0500",
        ),
        "later.eml",
    )
    earlier = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<earlier@x>", subject="Field Trip", date="Mon, 7 Mar 2022 09:00:00 -0500"),
        "earlier.eml",
    )

    db_session.refresh(later)
    db_session.refresh(earlier)
    assert later.thread_id == earlier.thread_id
    thread = db_session.get(CommunicationThread, later.thread_id)
    assert thread.first_message_at < thread.last_message_at


# --- thread merge when a later message links two prior threads -----------


def test_later_message_merges_two_existing_threads_into_one(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    a1 = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<a1@x>", subject="Topic A"), "a1.eml")
    a2 = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<a2@x>", in_reply_to="<a1@x>", subject="Re: Topic A"),
        "a2.eml",
    )
    b1 = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<b1@x>", subject="Topic B"), "b1.eml")
    b2 = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<b2@x>", in_reply_to="<b1@x>", subject="Re: Topic B"),
        "b2.eml",
    )

    db_session.refresh(a1)
    db_session.refresh(b1)
    thread_a_id, thread_b_id = a1.thread_id, b1.thread_id
    assert thread_a_id is not None and thread_b_id is not None
    assert thread_a_id != thread_b_id
    assert db_session.query(CommunicationThread).count() == 2

    # A message that references both chains (e.g. a reply-all that quotes
    # both prior conversations) links the two threads together.
    bridge = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(
            message_id="<bridge@x>",
            in_reply_to="<a2@x>",
            references=["<a1@x>", "<a2@x>", "<b1@x>", "<b2@x>"],
            subject="Re: Topic A",
        ),
        "bridge.eml",
    )

    for obj in (a1, a2, b1, b2, bridge):
        db_session.refresh(obj)

    canonical_id = min(thread_a_id, thread_b_id)
    assert {a1.thread_id, a2.thread_id, b1.thread_id, b2.thread_id, bridge.thread_id} == {canonical_id}

    # The merged-away thread row is gone -- no duplicate/orphaned thread rows.
    assert db_session.query(CommunicationThread).count() == 1
    surviving = db_session.get(CommunicationThread, canonical_id)
    assert surviving.message_count == 5


# --- idempotence ------------------------------------------------------------


def test_rebuild_is_idempotent_when_nothing_changed(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    parent = _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<p@x>"), "p.eml")
    reply = _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<r@x>", in_reply_to="<p@x>", subject="Re: IEP Meeting"),
        "r.eml",
    )
    db_session.refresh(parent)
    thread_id_before = parent.thread_id
    thread_count_before = db_session.query(CommunicationThread).count()

    rebuild_threads(db_session)
    db_session.commit()
    rebuild_threads(db_session)
    db_session.commit()

    db_session.refresh(parent)
    db_session.refresh(reply)
    assert parent.thread_id == thread_id_before
    assert reply.thread_id == thread_id_before
    assert db_session.query(CommunicationThread).count() == thread_count_before


# --- provenance is untouched by threading -----------------------------------


def test_threading_does_not_modify_raw_message_or_hash(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    source = _write(tmp_path, _eml(message_id="<p@x>"), "p.eml")
    original_hash = compute_sha256(source)
    parent = import_eml_file(
        db_session, vault, sample_case, source_file_path=source, original_filename="p.eml", actor="test-user"
    )
    db_session.commit()
    stored_hash_after_first_import = parent.sha256_hash
    stored_path_after_first_import = parent.stored_path

    reply_source = _write(
        tmp_path, _eml(message_id="<r@x>", in_reply_to="<p@x>", subject="Re: IEP Meeting"), "r.eml"
    )
    import_eml_file(
        db_session, vault, sample_case, source_file_path=reply_source, original_filename="r.eml", actor="test-user"
    )
    db_session.commit()

    db_session.refresh(parent)
    assert parent.sha256_hash == stored_hash_after_first_import == original_hash
    assert parent.stored_path == stored_path_after_first_import
    assert parent.subject is not None  # untouched evidentiary field
    assert (vault.root / parent.stored_path).read_bytes() == source.read_bytes()


def test_communications_count_unaffected_by_threading(
    db_session: Session, vault: VaultLayout, sample_case: Case, tmp_path: Path
):
    _import(db_session, vault, sample_case, tmp_path, _eml(message_id="<p@x>"), "p.eml")
    _import(
        db_session, vault, sample_case, tmp_path,
        _eml(message_id="<r@x>", in_reply_to="<p@x>", subject="Re: IEP Meeting"),
        "r.eml",
    )
    assert db_session.query(Communication).count() == 2
