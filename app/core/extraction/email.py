"""Email (.eml) extraction via the stdlib `email` package.

Page 1 is the message's key headers (From/To/Cc/Subject/Date) plus its
text body. Attachments are returned separately, as raw bytes — not as a
`document_pages` row — for the orchestration layer (service.py) to ingest
as their own independent child documents and extract recursively through
the same dispatcher. See docs/PHASE_2_PLAN.md §5.2. Only ever reads the
source file.
"""

from __future__ import annotations

import re
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
