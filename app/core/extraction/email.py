"""Email (.eml) extraction via the stdlib `email` package.

`extract()` below serves the Document extraction pipeline (Phase 2):
page 1 is the message's key headers (From/To/Cc/Subject/Date) plus its
text body, attachments returned separately as raw bytes for the
orchestration layer (service.py) to ingest as their own independent
child documents. See docs/PHASE_2_PLAN.md §5.2. Unchanged by, and
independent of, `parse_message()` below.

`parse_message()` (Communications Phase Step 3) is a second, richer
entry point for the same underlying parse, used by manual `.eml` upload
and (later) IMAP-fetched messages to populate a `Communication` row --
full structured headers (From/To/Cc/Bcc as parsed addresses,
Message-ID/In-Reply-To/References for threading, every raw header
preserved), and `body_text`/`body_html` kept as two separate optional
fields rather than the single flattened-and-merged text `extract()`
produces, since `Communication.body_text`/`body_html` are two distinct
columns. Both functions only ever read the source file -- neither writes
anything, and both share the same `_extract_attachments()` helper so
"what counts as an attachment" never drifts between the two paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

from app.core.extraction.types import ExtractedAttachment, ExtractedPage, ExtractionResult

_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def extract(file_path: Path) -> ExtractionResult:
    with file_path.open("rb") as f:
        message = BytesParser(policy=policy.default).parse(f)

    header_lines = [
        f"{field}: {message.get(field)}" for field in _HEADER_FIELDS if message.get(field)
    ]
    body = _extract_body(message)
    text = "\n".join(header_lines)
    if body:
        text = f"{text}\n\n{body}" if text else body

    page = ExtractedPage(
        page_number=1,
        text=text or None,
        extraction_method="native",
        char_count=len(text.strip()),
        needs_ocr=False,
    )
    attachments = _extract_attachments(message)
    return ExtractionResult(pages=[page], attachments=attachments)


def _extract_body(message) -> str:
    plain_part = message.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return plain_part.get_content().strip()

    html_part = message.get_body(preferencelist=("html",))
    if html_part is not None:
        # Minimal tag stripping, not full HTML parsing -- good enough to
        # make an HTML-only email's text searchable without adding an
        # HTML-parsing dependency for a fallback path.
        return _HTML_TAG_RE.sub(" ", html_part.get_content()).strip()

    return ""


def _extract_attachments(message) -> list[ExtractedAttachment]:
    attachments = []
    for part in message.iter_attachments():
        filename = part.get_filename() or "attachment"
        content = part.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        attachments.append(
            ExtractedAttachment(
                filename=filename,
                content=content,
                mime_type=part.get_content_type(),
            )
        )
    return attachments


@dataclass(frozen=True)
class ParsedEmailMessage:
    """The full structured parse of one `.eml` message (Communications
    Phase Step 3) -- everything `Communication` needs, populated once
    here so manual upload and (later) IMAP-fetched import share exactly
    one parsing definition. `raw_headers` preserves every header in file
    order, including any repeated header name, as `{"name", "value"}`
    pairs -- broader than `to`/`cc`/`bcc`/`message_id`/etc. below, which
    are the specific fields this application acts on.
    """

    subject: str | None
    from_address: str | None
    from_display_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    bcc_addresses: list[str]
    sent_at: datetime | None
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    raw_headers: list[dict[str, str]]
    body_text: str | None
    body_html: str | None
    attachments: list[ExtractedAttachment] = field(default_factory=list)


def parse_message(file_path: Path) -> ParsedEmailMessage:
    """Parse `file_path` (a `.eml` file) into a `ParsedEmailMessage`.

    Never modifies `file_path` -- opened for reading only, same
    guarantee as `extract()` above. Every field is best-effort: a
    missing or unparseable header simply yields `None`/`[]` for that
    field rather than raising, since a saved/forwarded/exported email
    routinely lacks one header or another and that is not a reason to
    fail the whole import.
    """
    with file_path.open("rb") as f:
        message = BytesParser(policy=policy.default).parse(f)

    from_address, from_display_name = _first_address(message.get("From"))
    message_id_header = message.get("Message-ID")
    in_reply_to_header = message.get("In-Reply-To")
    references_header = message.get("References")
    date_header = message.get("Date")

    return ParsedEmailMessage(
        subject=_header_text(message.get("Subject")),
        from_address=from_address,
        from_display_name=from_display_name,
        to_addresses=_address_list(message.get_all("To")),
        cc_addresses=_address_list(message.get_all("Cc")),
        bcc_addresses=_address_list(message.get_all("Bcc")),
        sent_at=getattr(date_header, "datetime", None),
        message_id=_header_text(message_id_header),
        in_reply_to=_header_text(in_reply_to_header),
        references=_header_text(references_header).split() if references_header else [],
        raw_headers=[{"name": name, "value": str(value)} for name, value in message.items()],
        body_text=_extract_body_text(message),
        body_html=_extract_body_html(message),
        attachments=_extract_attachments(message),
    )


def _header_text(header_value) -> str | None:
    if header_value is None:
        return None
    text = str(header_value).strip()
    return text or None


def _first_address(header_value) -> tuple[str | None, str | None]:
    """The address/display-name of the first (and normally only) address
    in a single-address header like From. Never raises on a malformed or
    missing header -- returns (None, None) instead.
    """
    if header_value is None or not getattr(header_value, "addresses", None):
        return None, None
    addr = header_value.addresses[0]
    return (addr.addr_spec or None), (addr.display_name or None)


def _address_list(header_values) -> list[str]:
    """Every address across all occurrences of a multi-recipient header
    (To/Cc/Bcc) -- a header can legally appear more than once, and each
    occurrence can itself list multiple comma-separated addresses.
    """
    if not header_values:
        return []
    return [
        addr.addr_spec
        for header in header_values
        for addr in getattr(header, "addresses", ())
        if addr.addr_spec
    ]


def _extract_body_text(message) -> str | None:
    plain_part = message.get_body(preferencelist=("plain",))
    if plain_part is None:
        return None
    return plain_part.get_content().strip() or None


def _extract_body_html(message) -> str | None:
    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        return None
    return html_part.get_content().strip() or None
