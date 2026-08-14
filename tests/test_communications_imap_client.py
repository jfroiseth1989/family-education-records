"""Tests for app/core/communications/imap_client.py -- the narrow,
read-only Yahoo IMAP client (Communications Phase Step 9).

Uses a fully in-process fake IMAP transport (`FakeImapTransport` below)
implementing the exact `_ImapTransport` protocol `ImapClient` depends on
-- no real network access, no real Yahoo account, ever. Deliberately
does *not* implement `store`/`copy`/`expunge`/`append`/any flag-mutating
method: if `ImapClient` ever tried to call one, the fake would raise
`AttributeError` immediately, which is itself the proof that no
mailbox-mutating command is ever issued -- see
`test_no_mutating_methods_exist_on_the_transport_protocol`.
"""

from __future__ import annotations

import imaplib
from datetime import date, datetime

import pytest

import app.core.communications.imap_client as imap_client_module
from app.core.communications.imap_client import (
    ImapAuthenticationError,
    ImapClient,
    ImapError,
    ImapMailboxAccessError,
    ImapNetworkError,
    ImapSearchCriteria,
    ImapTimeoutError,
    _decode_imap_utf7,
    _parse_list_response,
    _quote_astring,
)


def _eml(*, uid: int, sender: str, to: str = "parent@yahoo.com", cc: str = "",
         subject: str = "Hello", date_str: str = "Mon, 7 Mar 2022 14:30:00 -0500",
         body: str = "Body text.") -> tuple[int, bytes]:
    lines = [f"From: {sender}", f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    lines.append(f"Subject: {subject}")
    lines.append(f"Date: {date_str}")
    lines.append("")
    lines.append(body)
    return uid, ("\r\n".join(lines) + "\r\n").encode("utf-8")


class FakeImapTransport:
    """In-memory fake standing in for a real Yahoo IMAP connection.

    `mailboxes`: {folder_name: [(uid, raw_message_bytes), ...]}.
    `folder_defs`: [(name, attrs_string, delimiter)] for LIST.
    """

    def __init__(
        self,
        *,
        valid_credentials: tuple[str, str],
        mailboxes: dict[str, list[tuple[int, bytes]]] | None = None,
        folder_defs: list[tuple[str, str, str]] | None = None,
        fail_mode: str | None = None,
    ) -> None:
        self.valid_credentials = valid_credentials
        self.mailboxes = mailboxes or {}
        self.folder_defs = folder_defs or [("INBOX", "(\\HasNoChildren)", "/")]
        self.fail_mode = fail_mode
        self.logged_in = False
        self.logged_out = False
        self.selected_folder: str | None = None
        self.selected_readonly: bool | None = None
        self.commands: list[tuple] = []

    # --- the exact _ImapTransport surface -----------------------------

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if self.fail_mode == "auth":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        if self.fail_mode == "network":
            raise ConnectionRefusedError("connection refused")
        if self.fail_mode == "timeout":
            raise TimeoutError("timed out")
        if (user, password) != self.valid_credentials:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox, readonly):
        self.commands.append(("SELECT", mailbox, readonly))
        name = _unquote(mailbox)
        if name not in self.mailboxes:
            return ("NO", [b"Mailbox does not exist"])
        self.selected_folder = name
        self.selected_readonly = readonly
        return ("OK", [str(len(self.mailboxes[name])).encode()])

    def list(self):
        self.commands.append(("LIST",))
        lines = [f'({attrs}) "{delim}" "{name}"'.encode() for name, attrs, delim in self.folder_defs]
        return ("OK", lines)

    def uid(self, command, *args):
        self.commands.append(("UID", command, args))
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            return self._fetch(args)
        raise AssertionError(f"unexpected UID subcommand: {command}")

    def logout(self):
        self.commands.append(("LOGOUT",))
        self.logged_in = False
        self.logged_out = True
        return ("BYE", [b"LOGOUT completed"])

    # --- fake search/fetch interpretation ------------------------------

    def _search(self, args):
        import email as email_pkg

        messages = self.mailboxes.get(self.selected_folder, [])
        criteria = list(args)
        matched = []
        for uid, raw in messages:
            msg = email_pkg.message_from_bytes(raw)
            if _message_matches(msg, criteria, raw):
                matched.append(uid)
        matched.sort()
        line = " ".join(str(u) for u in matched).encode()
        return ("OK", [line])

    def _fetch(self, args):
        uid_set, _spec = args
        requested = {int(u) for u in uid_set.split(",")}
        messages = dict(self.mailboxes.get(self.selected_folder, []))
        data = []
        for uid in sorted(requested):
            raw = messages.get(uid)
            if raw is None:
                continue
            headers = _extract_headers(raw)
            meta = f"{uid} (UID {uid} BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE)] {{{len(headers)}}}".encode()
            data.append((meta, headers))
            data.append(b")")
        return ("OK", data)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _extract_headers(raw: bytes) -> bytes:
    header_part = raw.split(b"\r\n\r\n", 1)[0]
    return header_part + b"\r\n\r\n"


def _message_matches(msg, criteria: list[str], raw: bytes) -> bool:
    i = 0
    while i < len(criteria):
        key = criteria[i]
        if key == "ALL":
            i += 1
            continue
        if key in ("FROM", "TO", "CC", "SUBJECT", "TEXT"):
            value = _unquote(criteria[i + 1]).lower()
            haystack = (msg.get(key.capitalize() if key != "TEXT" else "Subject", "") or "").lower()
            if key == "TEXT":
                haystack = raw.decode("utf-8", "replace").lower()
            if value not in haystack:
                return False
            i += 2
            continue
        if key in ("SENTSINCE", "SENTBEFORE"):
            bound = datetime.strptime(criteria[i + 1], "%d-%b-%Y").date()
            date_header = msg.get("Date")
            msg_date = email_utils_parsedate(date_header)
            if msg_date is None:
                return False
            if key == "SENTSINCE" and msg_date < bound:
                return False
            if key == "SENTBEFORE" and msg_date >= bound:
                return False
            i += 2
            continue
        i += 1
    return True


def email_utils_parsedate(date_header):
    from email.utils import parsedate_to_datetime

    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).date()
    except Exception:
        return None


@pytest.fixture
def patch_transport(monkeypatch):
    """Return a function that installs `transport` as the module's
    default factory, so `ImapClient(...)` with no explicit
    `transport_factory` picks it up -- proving the same wiring
    `imap_service.open_connection()` uses in production also works for
    a fake transport.
    """

    def _install(transport) -> None:
        monkeypatch.setattr(imap_client_module, "_default_transport_factory", lambda host, port, timeout: transport)

    return _install


def _client() -> ImapClient:
    return ImapClient("fake.example.org", 993)


# --- connect / authenticate -------------------------------------------


def test_successful_authentication(patch_transport):
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "app-pw"))
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("parent@yahoo.com", "app-pw")
    assert transport.logged_in is True
    client.logout()
    assert transport.logged_out is True


def test_authentication_failure_raises_and_never_leaks_password(patch_transport):
    transport = FakeImapTransport(valid_credentials=("parent@yahoo.com", "app-pw"))
    patch_transport(transport)
    client = _client()
    with pytest.raises(ImapAuthenticationError) as excinfo:
        client.connect_and_authenticate("parent@yahoo.com", "wrong-password")
    assert "wrong-password" not in str(excinfo.value)
    assert "app-pw" not in str(excinfo.value)


def test_network_error_during_connect(patch_transport):
    transport = FakeImapTransport(valid_credentials=("a@b.com", "pw"), fail_mode="network")
    patch_transport(transport)
    with pytest.raises(ImapNetworkError):
        _client().connect_and_authenticate("a@b.com", "pw")


def test_timeout_during_connect(patch_transport):
    transport = FakeImapTransport(valid_credentials=("a@b.com", "pw"), fail_mode="timeout")
    patch_transport(transport)
    with pytest.raises(ImapTimeoutError):
        _client().connect_and_authenticate("a@b.com", "pw")


def test_connect_transport_factory_itself_failing(monkeypatch):
    def broken_factory(host, port, timeout):
        raise OSError("dns failure")

    monkeypatch.setattr(imap_client_module, "_default_transport_factory", broken_factory)
    with pytest.raises(ImapNetworkError):
        _client().connect_and_authenticate("a@b.com", "pw")


# --- folder listing -----------------------------------------------------


def test_list_folders_returns_dynamic_folders(patch_transport):
    transport = FakeImapTransport(
        valid_credentials=("a@b.com", "pw"),
        folder_defs=[
            ("INBOX", "\\HasNoChildren", "/"),
            ("Sent", "\\HasNoChildren \\Sent", "/"),
            ("Bulk Mail", "\\HasNoChildren \\Junk", "/"),
            ("Trash", "\\HasNoChildren \\Trash", "/"),
        ],
    )
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    folders = client.list_folders()
    names = {f.name for f in folders}
    assert names == {"INBOX", "Sent", "Bulk Mail", "Trash"}
    sent = next(f for f in folders if f.name == "Sent")
    assert "\\Sent" in sent.attributes
    client.logout()


def test_list_folders_never_hardcodes_inbox_sent_names(patch_transport):
    """A Yahoo account could use entirely different folder names --
    nothing about list_folders() should assume Inbox/Sent/Trash exist."""
    transport = FakeImapTransport(
        valid_credentials=("a@b.com", "pw"),
        folder_defs=[("Buzon de entrada", "\\HasNoChildren", "/"), ("Papelera", "\\HasNoChildren \\Trash", "/")],
    )
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    folders = client.list_folders()
    names = {f.name for f in folders}
    assert names == {"Buzon de entrada", "Papelera"}
    client.logout()


def test_unusual_folder_name_with_spaces_and_brackets(patch_transport):
    transport = FakeImapTransport(
        valid_credentials=("a@b.com", "pw"),
        folder_defs=[("[Yahoo]/Sent Mail", "\\HasNoChildren", "/")],
    )
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    folders = client.list_folders()
    assert folders[0].name == "[Yahoo]/Sent Mail"
    client.logout()


def test_folder_attributes_preserved_for_noselect():
    folder = _parse_list_response(rb'(\Noselect \HasChildren) "/" "Parent Folder"')
    assert folder is not None
    assert folder.is_selectable is False


def test_folder_modified_utf7_display_name_decodes_safely():
    assert _decode_imap_utf7("&AOk-tude") == "étude"
    # A segment that decodes to an odd byte count can't form valid
    # UTF-16BE code units -- this must never raise, only fall back to
    # the raw, undecoded name.
    assert _decode_imap_utf7("&AA-suffix") == "&AA-suffix"


# --- read-only selection --------------------------------------------------


def test_search_selects_mailbox_readonly(patch_transport):
    transport = FakeImapTransport(
        valid_credentials=("a@b.com", "pw"),
        mailboxes={"INBOX": [_eml(uid=1, sender="x@y.com")]},
    )
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    client.search(ImapSearchCriteria(folder="INBOX"))
    assert transport.selected_readonly is True
    client.logout()


def test_no_mutating_methods_exist_on_the_transport_protocol(patch_transport):
    """The fake transport deliberately has no store/copy/expunge/append
    method. If ImapClient ever tried to call one, this would raise
    AttributeError -- proving by construction that no Step 9 code path
    issues a mailbox-mutating command."""
    transport = FakeImapTransport(
        valid_credentials=("a@b.com", "pw"),
        mailboxes={"INBOX": [_eml(uid=1, sender="x@y.com")]},
    )
    for mutating_method in ("store", "copy", "expunge", "append", "setflags"):
        assert not hasattr(transport, mutating_method)
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    client.search(ImapSearchCriteria(folder="INBOX"))
    client.logout()


# --- search criteria ------------------------------------------------------


def _connected_client(patch_transport, mailboxes):
    transport = FakeImapTransport(valid_credentials=("a@b.com", "pw"), mailboxes=mailboxes)
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    return client, transport


def test_search_by_sender(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="Amanda Wagner <amanda@district.example.org>"),
            _eml(uid=2, sender="other@example.org"),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", sender="amanda"))
    assert result.total_matched == 1
    assert result.previews[0].uid == "1"
    client.logout()


def test_search_by_recipient(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", to="parent1@yahoo.com"),
            _eml(uid=2, sender="a@b.com", to="parent2@yahoo.com"),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", recipient="parent2"))
    assert result.total_matched == 1
    assert result.previews[0].uid == "2"
    client.logout()


def test_search_by_subject(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", subject="IEP Meeting Notice"),
            _eml(uid=2, sender="a@b.com", subject="Cafeteria Menu"),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", subject="IEP"))
    assert result.total_matched == 1
    assert result.previews[0].subject == "IEP Meeting Notice"
    client.logout()


def test_search_by_date_range(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", date_str="Mon, 1 Jan 2022 10:00:00 -0500"),
            _eml(uid=2, sender="a@b.com", date_str="Fri, 1 Jul 2022 10:00:00 -0500"),
            _eml(uid=3, sender="a@b.com", date_str="Sun, 1 Jan 2023 10:00:00 -0500"),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(
        ImapSearchCriteria(folder="INBOX", date_from=date(2022, 3, 1), date_to=date(2022, 9, 1))
    )
    assert result.total_matched == 1
    assert result.previews[0].uid == "2"
    client.logout()


def test_search_by_keywords(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="a@b.com", body="Please review the evaluation results."),
            _eml(uid=2, sender="a@b.com", body="See you at pickup."),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", keywords="evaluation"))
    assert result.total_matched == 1
    client.logout()


def test_combined_criteria(patch_transport):
    mailboxes = {
        "INBOX": [
            _eml(uid=1, sender="amanda@district.example.org", subject="IEP Meeting Notice"),
            _eml(uid=2, sender="amanda@district.example.org", subject="Cafeteria Menu"),
            _eml(uid=3, sender="other@example.org", subject="IEP Meeting Notice"),
        ]
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", sender="amanda", subject="IEP"))
    assert result.total_matched == 1
    assert result.previews[0].uid == "1"
    client.logout()


def test_zero_results(patch_transport):
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com")]}
    client, _ = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", sender="nobody-matches"))
    assert result.total_matched == 0
    assert result.previews == []
    assert result.truncated is False
    client.logout()


def test_bounded_and_paginated_results(patch_transport):
    mailboxes = {"INBOX": [_eml(uid=i, sender="a@b.com", subject=f"Msg {i}") for i in range(1, 31)]}
    client, _ = _connected_client(patch_transport, mailboxes)

    page1 = client.search(ImapSearchCriteria(folder="INBOX", limit=10, offset=0))
    assert page1.total_matched == 30
    assert len(page1.previews) == 10
    assert page1.truncated is True

    page4 = client.search(ImapSearchCriteria(folder="INBOX", limit=10, offset=29))
    assert len(page4.previews) == 1
    assert page4.truncated is False
    client.logout()


def test_search_never_fetches_more_than_the_limit(patch_transport):
    """Even a very large mailbox match must only ever trigger a bounded
    FETCH -- never a full-mailbox download."""
    mailboxes = {"INBOX": [_eml(uid=i, sender="a@b.com") for i in range(1, 5001)]}
    client, transport = _connected_client(patch_transport, mailboxes)
    result = client.search(ImapSearchCriteria(folder="INBOX", limit=5))
    assert result.total_matched == 5000
    assert len(result.previews) == 5
    fetch_calls = [c for c in transport.commands if c[0] == "UID" and c[1] == "FETCH"]
    assert len(fetch_calls) == 1
    requested_uid_set = fetch_calls[0][2][0]
    assert len(requested_uid_set.split(",")) == 5
    client.logout()


def test_uid_and_folder_identity_preserved(patch_transport):
    mailboxes = {
        "INBOX": [_eml(uid=42, sender="a@b.com")],
        "Sent": [_eml(uid=42, sender="a@b.com")],
    }
    client, _ = _connected_client(patch_transport, mailboxes)
    inbox_result = client.search(ImapSearchCriteria(folder="INBOX"))
    sent_result = client.search(ImapSearchCriteria(folder="Sent"))
    assert inbox_result.previews[0].uid == sent_result.previews[0].uid == "42"
    assert inbox_result.previews[0].folder == "INBOX"
    assert sent_result.previews[0].folder == "Sent"
    # Same numeric UID in two different folders is not the same message --
    # the durable identity must include folder, never UID alone.
    assert (inbox_result.previews[0].folder, inbox_result.previews[0].uid) != (
        sent_result.previews[0].folder,
        sent_result.previews[0].uid,
    )
    client.logout()


def test_malformed_search_input_does_not_break_out_of_quoting(patch_transport):
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com", subject="Normal Subject")]}
    client, transport = _connected_client(patch_transport, mailboxes)
    malicious = 'a" ALL DELETE INBOX "'
    result = client.search(ImapSearchCriteria(folder="INBOX", sender=malicious))
    # Neutralized to a literal (non-matching) search term -- no exception,
    # no unexpected additional command, zero matches.
    assert result.total_matched == 0
    search_calls = [c for c in transport.commands if c[0] == "UID" and c[1] == "SEARCH"]
    assert len(search_calls) == 1
    client.logout()


def test_search_input_with_crlf_cannot_inject_a_protocol_line(patch_transport):
    mailboxes = {"INBOX": [_eml(uid=1, sender="a@b.com")]}
    client, transport = _connected_client(patch_transport, mailboxes)
    injected = "a@b.com\r\nA1 LOGOUT\r\n"
    result = client.search(ImapSearchCriteria(folder="INBOX", sender=injected))
    assert result.total_matched == 0
    assert transport.logged_out is False
    client.logout()


def test_folder_that_does_not_exist_raises_mailbox_access_error(patch_transport):
    client, _ = _connected_client(patch_transport, {"INBOX": []})
    with pytest.raises(ImapMailboxAccessError):
        client.search(ImapSearchCriteria(folder="Nonexistent Folder"))
    client.logout()


def test_search_network_error(patch_transport):
    transport = FakeImapTransport(valid_credentials=("a@b.com", "pw"), mailboxes={"INBOX": []})
    patch_transport(transport)
    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")

    def broken_uid(command, *args):
        raise OSError("connection reset")

    transport.uid = broken_uid
    with pytest.raises(ImapNetworkError):
        client.search(ImapSearchCriteria(folder="INBOX"))


def test_not_connected_raises_before_touching_network():
    client = _client()
    with pytest.raises(ImapError):
        client.list_folders()


def test_logout_is_safe_to_call_when_never_connected():
    _client().logout()  # must not raise


def test_logout_is_safe_to_call_twice(patch_transport):
    client, transport = _connected_client(patch_transport, {"INBOX": []})
    client.logout()
    client.logout()
    assert transport.commands.count(("LOGOUT",)) == 1


# --- quoting helper -------------------------------------------------------


def test_quote_astring_escapes_quotes_and_backslashes():
    assert _quote_astring('he said "hi"') == '"he said \\"hi\\""'
    assert _quote_astring("back\\slash") == '"back\\\\slash"'


def test_quote_astring_strips_control_characters():
    assert _quote_astring("a\r\nb") == '"ab"'


def test_quote_astring_replaces_non_ascii():
    assert _quote_astring("café") == '"caf?"'


# --- Step 10 architecture boundary -----------------------------------------


def test_fetch_raw_message_uses_peek_and_returns_full_bytes(patch_transport):
    uid, raw = _eml(uid=7, sender="a@b.com", body="Full raw body text.")
    mailboxes = {"INBOX": [(uid, raw)]}
    transport = FakeImapTransport(valid_credentials=("a@b.com", "pw"), mailboxes=mailboxes)

    def fetch_full(args):
        uid_set, spec = args
        assert "BODY.PEEK[]" in spec
        return ("OK", [(f"{uid} FETCH".encode(), raw), b")"])

    original_fetch = transport._fetch
    transport._fetch = lambda args: fetch_full(args) if "BODY.PEEK[]" in args[1] else original_fetch(args)
    patch_transport(transport)

    client = _client()
    client.connect_and_authenticate("a@b.com", "pw")
    result = client.fetch_raw_message("INBOX", "7")
    assert result == raw
    client.logout()
