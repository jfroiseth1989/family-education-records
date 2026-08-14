"""Pure-function tests for app/core/communications/thread_grouping.py
(Communications Phase Step 4).

No database, no fixtures beyond plain data -- `group_messages()` and
`normalize_subject()` are pure functions of their inputs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.communications.thread_grouping import ThreadableMessage, group_messages, normalize_subject


def _dt(days: int = 0, hours: int = 0) -> datetime:
    return datetime(2024, 3, 1, tzinfo=timezone.utc) + timedelta(days=days, hours=hours)


def _msg(
    communication_id: int,
    *,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    subject: str | None = "Meeting",
    from_address: str | None = "sender@example.org",
    to_addresses: tuple[str, ...] = ("recipient@example.org",),
    cc_addresses: tuple[str, ...] = (),
    message_date: datetime | None = None,
    case_id: int | None = 1,
) -> ThreadableMessage:
    return ThreadableMessage(
        communication_id=communication_id,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        message_date=message_date,
        case_id=case_id,
    )


# --- normalize_subject ------------------------------------------------


def test_normalize_subject_strips_re_prefix():
    assert normalize_subject("Re: Hello") == "Hello"


def test_normalize_subject_strips_repeated_prefixes():
    assert normalize_subject("Re: Fwd: Re: Meeting Notes") == "Meeting Notes"


def test_normalize_subject_strips_prefix_with_no_space():
    assert normalize_subject("RE:Hello") == "Hello"


def test_normalize_subject_handles_fwd_and_fw_and_aw():
    assert normalize_subject("Fwd: Notice") == "Notice"
    assert normalize_subject("FW: Notice") == "Notice"
    assert normalize_subject("Aw: Notice") == "Notice"


def test_normalize_subject_handles_bracketed_reply_count():
    assert normalize_subject("Re[2]: Notice") == "Notice"


def test_normalize_subject_collapses_whitespace():
    assert normalize_subject("  Hello    World  ") == "Hello World"


def test_normalize_subject_leaves_subject_without_prefix_unchanged():
    assert normalize_subject("IEP Meeting Notice") == "IEP Meeting Notice"


def test_normalize_subject_handles_none_and_empty():
    assert normalize_subject(None) == ""
    assert normalize_subject("") == ""


# --- header-based grouping: normal parent/reply chains -----------------


def test_parent_and_reply_are_grouped_via_in_reply_to():
    parent = _msg(1, message_id="<a@x>", message_date=_dt(0))
    reply = _msg(2, message_id="<b@x>", in_reply_to="<a@x>", message_date=_dt(1))

    groups = group_messages([parent, reply])

    assert len(groups) == 1
    assert groups[0].communication_ids == (1, 2)
    assert groups[0].first_message_at == _dt(0)
    assert groups[0].last_message_at == _dt(1)


def test_three_message_chain_forms_one_thread():
    a = _msg(1, message_id="<a@x>", message_date=_dt(0))
    b = _msg(2, message_id="<b@x>", in_reply_to="<a@x>", message_date=_dt(1))
    c = _msg(3, message_id="<c@x>", in_reply_to="<b@x>", message_date=_dt(2))

    groups = group_messages([a, b, c])

    assert len(groups) == 1
    assert groups[0].communication_ids == (1, 2, 3)


def test_unrelated_root_messages_are_not_grouped():
    a = _msg(1, message_id="<a@x>", message_date=_dt(0))
    b = _msg(2, message_id="<b@x>", message_date=_dt(1))

    groups = group_messages([a, b])

    assert groups == []


# --- multiple References headers ---------------------------------------


def test_references_list_links_via_any_matching_ancestor():
    grandparent = _msg(1, message_id="<gp@x>", message_date=_dt(0))
    # `unknown@x` was never imported; `gp@x` was -- the message should
    # still link via whichever reference actually resolves.
    child = _msg(
        2,
        message_id="<child@x>",
        references=("<unknown@x>", "<gp@x>"),
        message_date=_dt(2),
    )

    groups = group_messages([grandparent, child])

    assert len(groups) == 1
    assert groups[0].communication_ids == (1, 2)


def test_references_links_earlier_ancestor_when_in_reply_to_target_missing():
    """The 'reply imported before its parent' case, at the pure-function
    level: In-Reply-To names a message not present in this batch, but an
    earlier ancestor in References is -- the link still forms.
    """
    grandparent = _msg(1, message_id="<gp@x>", message_date=_dt(0))
    reply = _msg(
        2,
        message_id="<reply@x>",
        in_reply_to="<missing-parent@x>",
        references=("<gp@x>", "<missing-parent@x>"),
        message_date=_dt(2),
    )

    groups = group_messages([grandparent, reply])

    assert len(groups) == 1
    assert groups[0].communication_ids == (1, 2)


# --- missing Message-ID (fallback) --------------------------------------


def test_headerless_messages_with_matching_signals_are_grouped():
    a = _msg(1, subject="Field Trip Permission", message_date=_dt(0), to_addresses=("parent@yahoo.com",))
    b = _msg(
        2,
        subject="Re: Field Trip Permission",
        message_date=_dt(1),
        from_address="parent@yahoo.com",
        to_addresses=("sender@example.org",),
    )

    groups = group_messages([a, b])

    assert len(groups) == 1
    assert groups[0].communication_ids == (1, 2)
    assert groups[0].subject_normalized == "Field Trip Permission"


def test_headerless_messages_too_far_apart_in_date_are_not_grouped():
    a = _msg(1, subject="Field Trip Permission", message_date=_dt(0), to_addresses=("parent@yahoo.com",))
    b = _msg(
        2,
        subject="Re: Field Trip Permission",
        message_date=_dt(100),
        from_address="parent@yahoo.com",
    )

    groups = group_messages([a, b])

    assert groups == []


def test_headerless_message_with_no_date_is_never_fallback_grouped():
    a = _msg(1, subject="Field Trip Permission", message_date=_dt(0), to_addresses=("parent@yahoo.com",))
    b = _msg(2, subject="Re: Field Trip Permission", message_date=None, from_address="parent@yahoo.com")

    groups = group_messages([a, b])

    assert groups == []


def test_message_with_message_id_is_never_fallback_grouped():
    """A message carrying real threading headers must never be merged by
    the fuzzy fallback, even if its own header doesn't resolve to a match.
    """
    a = _msg(1, message_id="<a@x>", subject="Field Trip Permission", message_date=_dt(0))
    b = _msg(
        2,
        subject="Re: Field Trip Permission",
        from_address=a.from_address,
        to_addresses=a.to_addresses,
        message_date=_dt(1),
    )
    # b is headerless and *could* fallback-match a's normalized subject +
    # participants -- but a has a Message-ID, so a itself is never a
    # candidate for fallback grouping. Only two headerless messages can
    # fallback-match each other, so this must not group.
    groups = group_messages([a, b])
    assert groups == []


# --- identical subjects, unrelated conversations ------------------------


def test_identical_subject_but_no_shared_participants_not_grouped():
    a = _msg(
        1,
        subject="Meeting",
        from_address="teacher-a@example.org",
        to_addresses=("family-a@example.org",),
        message_date=_dt(0),
    )
    b = _msg(
        2,
        subject="Meeting",
        from_address="teacher-b@example.org",
        to_addresses=("family-b@example.org",),
        message_date=_dt(0, hours=1),
    )

    groups = group_messages([a, b])

    assert groups == []


def test_two_header_linked_threads_with_identical_subject_stay_separate():
    a1 = _msg(1, message_id="<a1@x>", subject="Status Update", message_date=_dt(0))
    a2 = _msg(2, message_id="<a2@x>", in_reply_to="<a1@x>", subject="Re: Status Update", message_date=_dt(1))
    b1 = _msg(3, message_id="<b1@x>", subject="Status Update", message_date=_dt(0))
    b2 = _msg(4, message_id="<b2@x>", in_reply_to="<b1@x>", subject="Re: Status Update", message_date=_dt(1))

    groups = group_messages([a1, a2, b1, b2])

    assert len(groups) == 2
    ids = {g.communication_ids for g in groups}
    assert ids == {(1, 2), (3, 4)}


# --- out-of-order imports ------------------------------------------------


def test_grouping_is_independent_of_input_list_order():
    a = _msg(1, message_id="<a@x>", message_date=_dt(0))
    b = _msg(2, message_id="<b@x>", in_reply_to="<a@x>", message_date=_dt(1))
    c = _msg(3, message_id="<c@x>", in_reply_to="<b@x>", message_date=_dt(2))

    forward = group_messages([a, b, c])
    reversed_order = group_messages([c, b, a])
    shuffled = group_messages([b, a, c])

    expected = {(1, 2, 3)}
    assert {g.communication_ids for g in forward} == expected
    assert {g.communication_ids for g in reversed_order} == expected
    assert {g.communication_ids for g in shuffled} == expected


def test_fallback_grouping_independent_of_input_order_for_dated_messages():
    a = _msg(1, subject="Trip", message_date=_dt(2), to_addresses=("x@example.org",))
    b = _msg(2, subject="Re: Trip", message_date=_dt(0), from_address="x@example.org")
    c = _msg(3, subject="Re: Trip", message_date=_dt(1), from_address="x@example.org")

    forward = group_messages([a, b, c])
    reversed_order = group_messages([c, b, a])

    assert {g.communication_ids for g in forward} == {(1, 2, 3)}
    assert {g.communication_ids for g in reversed_order} == {(1, 2, 3)}


# --- idempotence (pure-function level: stable given identical input) ----


def test_group_messages_is_deterministic_across_repeated_calls():
    a = _msg(1, message_id="<a@x>", message_date=_dt(0))
    b = _msg(2, message_id="<b@x>", in_reply_to="<a@x>", message_date=_dt(1))
    messages = [a, b]

    first = group_messages(messages)
    second = group_messages(messages)

    assert first == second
