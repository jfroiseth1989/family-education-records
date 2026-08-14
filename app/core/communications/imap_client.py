"""A narrow, read-only IMAP client (Communications Phase Step 9).

FERChronos's first live network integration. Deliberately built as a
small abstraction exposing only the operations Step 9 actually needs --
connect/authenticate, list folders, select a mailbox read-only, search,
fetch bounded preview metadata, and log out. There is no method on
`ImapClient` for STORE, COPY, MOVE, EXPUNGE, DELETE, APPEND, or any
flag-modifying command -- those verbs simply do not exist anywhere in
this module, not merely "unused." Every mailbox SELECT this module ever
issues passes `readonly=True`, so even a bug elsewhere in this
application cannot turn a preview into an accidental mailbox mutation
(e.g. marking a message \\Seen).

Talks to the mailbox through a small `_ImapTransport` protocol rather
than calling `imaplib` directly everywhere, so tests can substitute a
fake in-memory transport and never need a real Yahoo account or network
access -- see tests/test_communications_imap_client.py.

No credential (the Yahoo app password) is ever written into a message
string this module constructs, logged, or included in any exception
text -- every error message below is a fixed, safe string; the
underlying exception is chained via `from exc` for local traceback
inspection only, never rendered to a user or written to a log record
that includes request data.

**UID scope.** An IMAP UID is only unique *within its folder* on a given
account (RFC 3501 §2.3.1.1) -- never treated as globally unique
anywhere in this application. `ImapMessagePreview` always carries both
`folder` and `uid` together for exactly this reason; Step 10's future
import step must key any "already seen this message" check off
`(account, folder, uid)`, never `uid` alone.

**Step 10 architecture boundary.** `fetch_raw_message()` exists so a
later bulk-import engine can request one message's complete, unmodified
RFC822 bytes by `(folder, uid)` without this module needing to change.
No Step 9 route calls it, and it is never used to write anything into
the vault or `communications` -- Step 9 is inspection only.
"""

from __future__ import annotations

import base64
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesHeaderParser
from typing import Protocol

from app.core.extraction.email import _address_list, _first_address, _header_text

YAHOO_IMAP_HOST = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_SEARCH_LIMIT = 25
MAX_SEARCH_LIMIT = 200

_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


class ImapError(Exception):
    """Base for every error this module raises. Message text is always a
    fixed, safe string -- never built from raw server response bytes or
    from any credential -- so it is always safe to show directly to the
    user.
    """


class ImapCredentialUnavailableError(ImapError):
    """No secure credential is available for this account (a keyring
    miss -- the stored `credential_ref` names nothing, e.g. because the
    OS secret store was cleared outside FERChronos)."""


class ImapAuthenticationError(ImapError):
    """The mail server rejected the supplied app password."""


class ImapNetworkError(ImapError):
    """Could not reach, or lost, the connection to the mail server (DNS,
    TCP, or TLS failure; an unexpected disconnect)."""


class ImapTimeoutError(ImapError):
    """The mail server did not respond within the configured timeout."""


class ImapMailboxAccessError(ImapError):
    """A folder could not be listed, selected, or searched, or a search
    request was rejected by the server."""


@dataclass(frozen=True)
class ImapFolder:
    """One IMAP folder, as returned by LIST.

    `name` is the raw IMAP mailbox name -- exactly what must be passed
    back to `search()`/`fetch_raw_message()` to select this folder again.
    `display_name` is a best-effort decode (IMAP "modified UTF-7", RFC
    3501 §5.1.3) safe to render in the UI; decoding never raises -- an
    unparseable name falls back to the raw name unchanged rather than
    breaking folder listing over one oddly-encoded entry.
    """

    name: str
    display_name: str
    delimiter: str | None
    attributes: tuple[str, ...]

    @property
    def is_selectable(self) -> bool:
        return "\\Noselect" not in self.attributes


@dataclass(frozen=True)
class ImapMessagePreview:
    """Metadata-only preview of one mailbox message -- never the message
    body, and never anything written to disk. `folder` + `uid` together
    are the durable identity a future import step needs; see the module
    docstring's UID-scope note.
    """

    uid: str
    folder: str
    from_address: str | None
    from_display_name: str | None
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    subject: str | None
    date: datetime | None


@dataclass(frozen=True)
class ImapSearchCriteria:
    """A bounded mailbox search request.

    Every text field is matched with IMAP's own `FROM`/`TO`/`CC`/
    `SUBJECT`/`TEXT` search keys (substring match, server-side) -- never
    downloaded and filtered locally. There is deliberately no
    `has_attachments` field: Yahoo/IMAP has no native, reliable
    server-side "has an attachment" search key, and approximating one
    would require fetching each candidate message's full structure or
    body, which Step 9 explicitly avoids doing across a whole mailbox.

    `limit`/`offset` bound how many *preview* fetches this search
    performs -- `search()` never fetches preview metadata for more than
    `limit` messages no matter how many the mailbox-wide match count is.
    """

    folder: str
    date_from: date | None = None
    date_to: date | None = None
    sender: str = ""
    recipient: str = ""
    cc: str = ""
    subject: str = ""
    keywords: str = ""
    limit: int = DEFAULT_SEARCH_LIMIT
    offset: int = 0


@dataclass(frozen=True)
class ImapSearchResult:
    """`total_matched` is the server's full match count for the search
    (cheap -- SEARCH returns UIDs, not messages); `previews` is only the
    bounded page actually fetched. `truncated` is True whenever more
    matches exist beyond this page, so the UI can show "showing 25 of
    412" rather than implying the mailbox only had 25 matches.
    """

    total_matched: int
    previews: list[ImapMessagePreview]
    truncated: bool


class _ImapTransport(Protocol):
    """The exact subset of `imaplib.IMAP4_SSL`'s interface this module
    depends on. A test double implementing this same shape stands in for
    a real Yahoo connection with no network access required. Note what's
    *not* here: no `store`, `copy`, `expunge`, `append`, or any
    flag-mutating method -- this module never calls them because they
    are never part of the contract it depends on in the first place.
    """

    def login(self, user: str, password: str) -> tuple: ...
    def select(self, mailbox: str, readonly: bool) -> tuple: ...
    def list(self) -> tuple: ...
    def uid(self, command: str, *args: str) -> tuple: ...
    def logout(self) -> tuple: ...


def _default_transport_factory(host: str, port: int, timeout: float) -> _ImapTransport:
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


class ImapClient:
    """A single narrow, read-only IMAP session.

    One instance wraps one connection. `connect_and_authenticate()` must
    be called before any other method; `logout()` should always be
    called when done (safe to call more than once, and safe to call on a
    client that never successfully connected).
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport_factory=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # `transport_factory` defaults to `None` and is resolved against
        # the *module-level* `_default_transport_factory` name here, at
        # call time, rather than being bound as an ordinary default
        # argument value -- an ordinary default would capture the real
        # function once at class-definition time, making it impossible
        # for a test to substitute a fake transport by patching the
        # module attribute (a ordinary default keeps referencing the
        # original object regardless of what the module name is
        # rebound to later). This is the only reason production code
        # never has to pass `transport_factory` explicitly while tests
        # still can.
        self._host = host
        self._port = port
        self._transport_factory = transport_factory or _default_transport_factory
        self._timeout = timeout
        self._transport: _ImapTransport | None = None

    def connect_and_authenticate(self, email_address: str, app_password: str) -> None:
        if self._transport is not None:
            raise ImapError("This client is already connected.")

        try:
            transport = self._transport_factory(self._host, self._port, self._timeout)
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except (OSError, ssl.SSLError) as exc:
            raise ImapNetworkError("Could not reach the mail server.") from exc

        try:
            transport.login(email_address, app_password)
        except imaplib.IMAP4.error as exc:
            _safe_logout(transport)
            raise ImapAuthenticationError(
                "The mail server rejected the app password for this account."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            _safe_logout(transport)
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except (OSError, ssl.SSLError) as exc:
            _safe_logout(transport)
            raise ImapNetworkError("Could not reach the mail server.") from exc

        self._transport = transport

    def list_folders(self) -> list[ImapFolder]:
        transport = self._require_connected()
        try:
            typ, data = transport.list()
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except OSError as exc:
            raise ImapNetworkError("Lost connection to the mail server.") from exc

        if typ != "OK":
            raise ImapMailboxAccessError("Could not list mailbox folders.")

        folders = []
        for raw in data or []:
            folder = _parse_list_response(raw)
            if folder is not None:
                folders.append(folder)
        return folders

    def search(self, criteria: ImapSearchCriteria) -> ImapSearchResult:
        transport = self._require_connected()
        self._select_readonly(transport, criteria.folder)

        search_args = _build_search_args(criteria)
        try:
            typ, data = transport.uid("SEARCH", *search_args)
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except OSError as exc:
            raise ImapNetworkError("Lost connection to the mail server.") from exc

        if typ != "OK":
            raise ImapMailboxAccessError("The mail server rejected this search.")

        raw_uids = data[0].split() if data and data[0] else []
        # Newest first, matching how the rest of this application always
        # orders communications -- SEARCH itself returns UIDs in
        # ascending order.
        all_uids = list(reversed(raw_uids))
        total_matched = len(all_uids)

        limit = max(1, min(criteria.limit, MAX_SEARCH_LIMIT))
        offset = max(0, criteria.offset)
        page_uids = all_uids[offset : offset + limit]

        previews = self._fetch_previews(transport, criteria.folder, page_uids)
        truncated = (offset + len(previews)) < total_matched

        return ImapSearchResult(total_matched=total_matched, previews=previews, truncated=truncated)

    def fetch_raw_message(self, folder: str, uid: str) -> bytes:
        """Return one message's complete, unmodified raw RFC822 bytes by
        `(folder, uid)`. Uses `BODY.PEEK[]` so the server's `\\Seen` flag
        is never set merely by fetching it -- read-only in effect, not
        just by the read-only SELECT.

        This is the Step 10 architecture boundary described in the
        module docstring: it exists so a future bulk-import engine can
        request raw message bytes without this module changing, but no
        Step 9 route calls it, and nothing in Step 9 uses its result to
        write to the vault, `communications`, or any custody ledger.
        """
        transport = self._require_connected()
        self._select_readonly(transport, folder)

        uid_arg = _validate_and_join_uids([uid])
        try:
            typ, data = transport.uid("FETCH", uid_arg, "(BODY.PEEK[])")
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except OSError as exc:
            raise ImapNetworkError("Lost connection to the mail server.") from exc

        if typ != "OK" or not data:
            raise ImapMailboxAccessError("Could not retrieve this message.")

        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                return item[1]
        raise ImapMailboxAccessError("Could not retrieve this message.")

    def logout(self) -> None:
        if self._transport is None:
            return
        _safe_logout(self._transport)
        self._transport = None

    def _select_readonly(self, transport: _ImapTransport, folder: str) -> None:
        folder_arg = _quote_astring(folder)
        try:
            typ, _ = transport.select(folder_arg, readonly=True)
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except OSError as exc:
            raise ImapNetworkError("Lost connection to the mail server.") from exc
        if typ != "OK":
            raise ImapMailboxAccessError("Could not open the selected folder.")

    def _fetch_previews(
        self, transport: _ImapTransport, folder: str, uids: list[bytes]
    ) -> list[ImapMessagePreview]:
        if not uids:
            return []

        decoded_uids = [u.decode("ascii") for u in uids]
        uid_arg = _validate_and_join_uids(decoded_uids)
        try:
            typ, data = transport.uid(
                "FETCH", uid_arg, "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])"
            )
        except (TimeoutError, socket.timeout) as exc:
            raise ImapTimeoutError("The mail server did not respond in time.") from exc
        except OSError as exc:
            raise ImapNetworkError("Lost connection to the mail server.") from exc

        if typ != "OK":
            raise ImapMailboxAccessError("Could not retrieve message previews.")

        previews = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            meta_line, header_bytes = item
            uid = _extract_uid(meta_line)
            if uid is None:
                continue
            previews.append(_build_preview(folder, uid, header_bytes))

        # FETCH responses aren't guaranteed to arrive in the requested
        # UID order -- resort to match the newest-first order search()
        # already established.
        order = {uid: index for index, uid in enumerate(decoded_uids)}
        previews.sort(key=lambda p: order.get(p.uid, len(order)))
        return previews

    def _require_connected(self) -> _ImapTransport:
        if self._transport is None:
            raise ImapError("Not connected -- call connect_and_authenticate() first.")
        return self._transport


def _safe_logout(transport: _ImapTransport) -> None:
    try:
        transport.logout()
    except Exception:
        pass


def _build_search_args(criteria: ImapSearchCriteria) -> list[str]:
    args: list[str] = []
    if criteria.sender.strip():
        args += ["FROM", _quote_astring(criteria.sender.strip())]
    if criteria.recipient.strip():
        args += ["TO", _quote_astring(criteria.recipient.strip())]
    if criteria.cc.strip():
        args += ["CC", _quote_astring(criteria.cc.strip())]
    if criteria.subject.strip():
        args += ["SUBJECT", _quote_astring(criteria.subject.strip())]
    if criteria.keywords.strip():
        args += ["TEXT", _quote_astring(criteria.keywords.strip())]
    if criteria.date_from is not None:
        args += ["SENTSINCE", _format_imap_date(criteria.date_from)]
    if criteria.date_to is not None:
        args += ["SENTBEFORE", _format_imap_date(criteria.date_to + timedelta(days=1))]
    return args or ["ALL"]


def _format_imap_date(value: date) -> str:
    return f"{value.day:02d}-{_MONTH_ABBREVIATIONS[value.month - 1]}-{value.year:04d}"


def _quote_astring(value: str) -> str:
    """Safely quote `value` for use as one IMAP command argument.

    Strips control characters (in particular CR/LF, which could
    otherwise inject an entirely separate protocol line into the
    command stream -- the actual security-relevant risk here) and
    escapes backslash/double-quote so the value can never break out of
    its own quoted string. Restricted to what IMAP quoted strings can
    carry directly (7-bit) -- a non-ASCII character is replaced with
    "?" rather than attempting IMAP literal/charset framing, which this
    narrow client does not implement; a search or folder name with
    non-ASCII characters may therefore not match exactly, a documented
    limitation (docs/COMMUNICATIONS_PLAN.md Step 9), not a silent
    correctness bug -- it degrades to "no match" rather than ever
    executing something other than the intended command.
    """
    sanitized = "".join(ch for ch in value if ch >= " " or ch == "\t")
    ascii_safe = sanitized.encode("ascii", "replace").decode("ascii")
    escaped = ascii_safe.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_UID_RE = re.compile(r"^[0-9]+$")


def _validate_and_join_uids(uids: list[str]) -> str:
    """Build a comma-separated UID set for a UID FETCH command,
    rejecting anything that isn't purely digits -- UIDs are always
    server-assigned integers, so this is a hard allowlist, not a
    best-effort sanitizer: any unexpected shape simply fails loudly.
    """
    for uid in uids:
        if not _UID_RE.match(uid):
            raise ImapError("Received an unexpected message identifier from the mail server.")
    return ",".join(uids)


_UID_IN_META_RE = re.compile(rb"UID (\d+)")


def _extract_uid(meta_line: bytes) -> str | None:
    match = _UID_IN_META_RE.search(meta_line)
    return match.group(1).decode("ascii") if match else None


def _build_preview(folder: str, uid: str, header_bytes: bytes) -> ImapMessagePreview:
    message = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
    from_address, from_display_name = _first_address(message.get("From"))
    date_header = message.get("Date")
    return ImapMessagePreview(
        uid=uid,
        folder=folder,
        from_address=from_address,
        from_display_name=from_display_name,
        to_addresses=tuple(_address_list(message.get_all("To"))),
        cc_addresses=tuple(_address_list(message.get_all("Cc"))),
        subject=_header_text(message.get("Subject")),
        date=getattr(date_header, "datetime", None),
    )


_LIST_RESPONSE_RE = re.compile(
    rb'^\((?P<attrs>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|(?P<delim_nil>NIL))\s+(?P<name>.+)$'
)


def _parse_list_response(raw) -> ImapFolder | None:
    if not isinstance(raw, bytes):
        return None
    match = _LIST_RESPONSE_RE.match(raw)
    if match is None:
        return None

    attrs = tuple(a for a in match.group("attrs").decode("ascii", "replace").split() if a)
    delimiter = None if match.group("delim_nil") else match.group("delim").decode("ascii", "replace")
    name = _unquote_astring(match.group("name").strip())
    display_name = _decode_imap_utf7(name)
    return ImapFolder(name=name, display_name=display_name, delimiter=delimiter, attributes=attrs)


def _unquote_astring(raw: bytes) -> str:
    text = raw.decode("ascii", "replace").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return text


def _decode_imap_utf7(name: str) -> str:
    """Best-effort decode of IMAP "modified UTF-7" mailbox names (RFC
    3501 §5.1.3) for safe display. Never raises -- an unparseable name
    is returned unchanged rather than breaking folder listing over one
    oddly-encoded entry, since a folder's *display* name is only ever a
    label here, nothing structural depends on the decode succeeding
    (the raw `name` used for SELECT/SEARCH is untouched by this).
    """
    try:
        return _decode_imap_utf7_strict(name)
    except Exception:
        return name


def _decode_imap_utf7_strict(name: str) -> str:
    result = []
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if ch != "&":
            result.append(ch)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end == -1:
            end = n
        segment = name[i + 1 : end]
        if segment == "":
            result.append("&")
        else:
            modified_b64 = segment.replace(",", "/")
            padding = "=" * (-len(modified_b64) % 4)
            raw = base64.b64decode(modified_b64 + padding)
            result.append(raw.decode("utf-16-be"))
        i = end + 1
    return "".join(result)
