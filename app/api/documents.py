"""Document routes: upload (ingest), view, integrity check, version linking.

See docs/ARCHITECTURE.md §3.1 (ingestion) and §3.2 (versioning / custody).
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_db, get_vault
from app.core.annotations.service import count_document_annotations
from app.core.custody import verify_document_integrity, write_custody_event
from app.core.document_dates import InvalidDateRangeError, set_document_date
from app.core.document_date_suggestion import DocumentDateSuggestions, suggest_document_dates
from app.core.document_notes_suggestion import NotesSuggestion, compose_notes_summary
from app.core.document_source_suggestion import SourceSuggestion, suggest_document_source
from app.core.document_type_suggestion import DocumentTypeSuggestion, suggest_document_type
from app.core.extraction.dispatcher import get_extractor, is_image_extension
from app.core.extraction.service import extract_document
from app.core.ingestion.service import DuplicateDocumentError, ingest_document
from app.core.ingestion.versioning import VersionLinkError, link_as_new_version
from app.core.ocr.queue import enqueue_ocr_job
from app.core.ocr.text import effective_text
from app.core.tagging import list_case_tags
from app.core.vault import VaultLayout
from app.db.models import Case, Document, DocumentDatePrecision, DocumentDateSource, DocumentType

router = APIRouter(tags=["documents"])


def _get_case_or_404(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Student {case_id} not found.")
    return case


def _get_document_or_404(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    return document


def _maybe_enqueue_ocr(db: Session, document: Document, actor: str) -> None:
    """Queue `document` for OCR if extraction flagged any page as needing it.

    Runs right after `extract_document()`, in the same request — enqueuing
    itself is cheap (one row, no OCR execution), unlike OCR itself, which
    runs in the background (see app/jobs/worker.py).
    """
    if document.needs_ocr:
        enqueue_ocr_job(db, document, actor)


def _save_upload_to_temp(upload: UploadFile) -> Path:
    """Spool an uploaded file to a temp path so ingestion can hash/copy it.

    The temp file is a working copy of what the browser sent — it is not
    the "original" in the chain-of-custody sense; the copy ingestion makes
    into `originals/` from this temp file is. Callers are responsible for
    deleting the temp file once ingestion has copied it into the vault.
    """
    suffix = Path(upload.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(upload.file, tmp)
        return Path(tmp.name)


def _parse_optional_date(raw: str, field_label: str) -> date | None:
    """Parse one optional YYYY-MM-DD form field, shared by every date field
    on the ingestion forms (document date, its range end, and date
    received). An empty value means "unknown/not applicable" -- a fully
    valid choice; an unparseable non-empty value is rejected with a 400
    rather than silently ignored.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_label} '{value}' (expected YYYY-MM-DD).",
        ) from exc


def _parse_document_date_form(
    document_date_raw: str, precision_raw: str, range_end_raw: str
) -> tuple[date | None, DocumentDatePrecision, date | None]:
    """Parse the ingestion form's document-date fields: date, precision,
    range end.

    Cross-field validation (e.g. "range" precision requires a range end,
    exact/approximate must not have one, range end must not precede the
    start) happens later, inside `ingest_document` via
    `app.core.document_dates.validate_date_combination`, and is surfaced
    to the caller as `InvalidDateRangeError` -> 400 (see both routes below).
    """
    try:
        precision = DocumentDatePrecision(precision_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid document date precision '{precision_raw}'.",
        ) from exc

    parsed_date = _parse_optional_date(document_date_raw, "document date")
    parsed_range_end = _parse_optional_date(range_end_raw, "document date range end")

    return parsed_date, precision, parsed_range_end


def _collect_field_provenance(**sources: str) -> dict[str, str]:
    """Build the `field_provenance` map `ingest_document` records on the
    `imported` custody event, from a set of hidden `<field>_source` form
    values (see app/web/static/document_preview.js) -- one of "suggested",
    "accepted", "edited", "manual", or "cleared" per field. A field whose
    source wasn't submitted (blank -- e.g. an older client, or a form that
    doesn't offer suggestions at all) is simply omitted rather than
    recorded as an uninteresting default, matching `ingest_document`'s own
    "None/empty omits provenance entirely" contract.
    """
    return {field: value for field, value in sources.items() if value.strip()}


@router.post("/cases/{case_id}/documents")
def upload_document(
    request: Request,
    case_id: int,
    file: UploadFile,
    source: str = Form(""),
    document_type_id: str = Form(""),
    notes: str = Form(""),
    document_date: str = Form(""),
    document_date_precision: str = Form("exact"),
    document_date_range_end: str = Form(""),
    date_received: str = Form(""),
    document_type_source: str = Form(""),
    document_date_source: str = Form(""),
    date_received_source: str = Form(""),
    source_source: str = Form(""),
    notes_source: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Ingest an uploaded file as a new document in the given case."""
    case = _get_case_or_404(db, case_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    parsed_type_id = int(document_type_id) if document_type_id.strip() else None
    parsed_date, date_precision, parsed_range_end = _parse_document_date_form(
        document_date, document_date_precision, document_date_range_end
    )
    parsed_received = _parse_optional_date(date_received, "date received")
    field_provenance = _collect_field_provenance(
        document_type_id=document_type_source,
        document_date=document_date_source,
        date_received=date_received_source,
        source=source_source,
        notes=notes_source,
    )

    temp_path = _save_upload_to_temp(file)
    try:
        try:
            document = ingest_document(
                db,
                vault,
                case,
                source_file_path=temp_path,
                original_filename=file.filename,
                actor=actor,
                source=source.strip() or None,
                document_type_id=parsed_type_id,
                notes=notes.strip() or None,
                mime_type=file.content_type,
                document_date=parsed_date,
                document_date_precision=date_precision,
                document_date_range_end=parsed_range_end,
                date_received=parsed_received,
                field_provenance=field_provenance,
            )
        except DuplicateDocumentError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidDateRangeError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Extraction runs in the same request, right after a successful
        # ingest, but never raises out to here -- an unparseable or
        # unsupported file is recorded via extraction_status/extraction_error
        # on the document itself (approved decision 6), not an exception.
        extract_document(db, vault, document, actor)
        _maybe_enqueue_ocr(db, document, actor)

        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.post("/cases/{case_id}/documents/preview")
def preview_document(
    case_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Local, pre-ingestion drop-zone preview: suggest a document type,
    source, dates, and a Notes summary from the file's name and (for
    text-bearing formats) its native extracted text -- filename-only for
    an image, since OCR is a background job and deliberately never runs
    synchronously here (see `_native_text_sample`'s docstring).

    Writes nothing: no document row, no vault file, no hash, no custody
    event. The temp file this spools to is always deleted before
    returning, success or failure. This only ever returns a suggestion
    for the browser to offer -- see app/web/static/document_preview.js --
    never anything applied or persisted server-side; the real ingestion
    route (`upload_document`, above) is what actually saves whatever the
    human ultimately submits.
    """
    _get_case_or_404(db, case_id)  # 404s for an unknown case; nothing else about it is used

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    temp_path = _save_upload_to_temp(file)
    try:
        text_sample = _native_text_sample(file.filename, temp_path)
    finally:
        temp_path.unlink(missing_ok=True)

    is_email = Path(file.filename).suffix.lower() == ".eml"
    document_types = list(
        db.scalars(select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)).all()
    )

    # Computed as objects first, not serialized dicts, so Notes
    # composition (which needs the type/date/source *objects*, not their
    # JSON shape) can reuse exactly what type/date/source suggestion
    # already found -- never a second, independent read of the text.
    type_suggestion = suggest_document_type(file.filename, text_sample)
    date_suggestions = suggest_document_dates(text_sample, is_email=is_email)
    source_suggestion = suggest_document_source(text_sample, is_email=is_email)
    notes_suggestion = compose_notes_summary(
        filename=file.filename,
        text_sample=text_sample,
        type_suggestion=type_suggestion,
        document_date=date_suggestions.document_date,
        informational=date_suggestions.informational,
        source=source_suggestion,
    )

    payload = {
        "document_type": _serialize_type_suggestion(type_suggestion, document_types),
        **_serialize_date_suggestions(date_suggestions),
        "source": _serialize_source_suggestion(source_suggestion),
        "notes": _serialize_notes_suggestion(notes_suggestion),
    }
    return JSONResponse(payload)


def _serialize_type_suggestion(
    suggestion: DocumentTypeSuggestion | None, document_types: list[DocumentType]
) -> dict | None:
    """Resolve an already-computed type suggestion to a concrete
    DocumentType row (not done in the suggestion module, which is
    deliberately free of any DB dependency) so the caller can offer a
    one-click "accept" that posts a real type_id.
    """
    if suggestion is None:
        return None

    matched_type = next((dt for dt in document_types if dt.name == suggestion.type_name), None)
    if matched_type is None:
        # Should not happen -- every trigger name matches a seeded
        # DocumentType exactly (see app/db/seed.py) -- but never crash
        # the page over it; just don't offer an unactionable suggestion.
        return None

    return {
        "type_id": matched_type.type_id,
        "type_name": suggestion.type_name,
        "matched_terms": list(suggestion.matched_terms),
    }


def _resolve_type_suggestion(
    filename: str, text_sample: str, document_types: list[DocumentType]
) -> dict | None:
    """Locally suggest a document type from `filename`/`text_sample`, or
    None -- used by the post-ingestion suggestion on the document detail
    page (`_compute_type_suggestion`, below). See
    app/core/document_type_suggestion.py for the matching rules and the
    standing no-AI/no-network guarantee.
    """
    return _serialize_type_suggestion(suggest_document_type(filename, text_sample), document_types)


def _compute_type_suggestion(
    document: Document, document_types: list[DocumentType]
) -> dict | None:
    """Locally suggest a document type for `document`, or None.

    Only ever offered when the document has no type set yet (FERChronos
    Step 5.6) -- an already-typed document is never second-guessed or
    silently recategorized. Uses only the filename and the first few
    pages' effective (extracted/OCR) text.
    """
    if document.document_type_id is not None:
        return None

    text_sample = "\n".join(
        text
        for text in (
            effective_text(page).text
            for page in sorted(document.pages, key=lambda p: p.page_number)[:3]
        )
        if text
    )
    return _resolve_type_suggestion(document.original_filename, text_sample, document_types)


def _serialize_date_field(suggestion) -> dict | None:
    if suggestion is None:
        return None
    return {
        "value": suggestion.value.isoformat(),
        "range_end": suggestion.range_end.isoformat() if suggestion.range_end else None,
        "precision": suggestion.precision,
        "matched_phrase": suggestion.matched_phrase,
    }


def _serialize_date_suggestions(suggestions: DocumentDateSuggestions) -> dict:
    return {
        "document_date": _serialize_date_field(suggestions.document_date),
        "date_received": _serialize_date_field(suggestions.date_received),
        "informational_dates": [
            {"label": note.label, "value": note.value.isoformat(), "matched_phrase": note.matched_phrase}
            for note in suggestions.informational
        ],
    }


def _resolve_date_suggestions(text_sample: str, *, is_email: bool) -> dict:
    """Locally suggest Document Date / Date Received values (and any
    purely informational dates) from `text_sample` -- used by the
    deferred suggestion shown on the document detail page once OCR text
    becomes available. See app/core/document_date_suggestion.py for the
    matching rules.
    """
    return _serialize_date_suggestions(suggest_document_dates(text_sample, is_email=is_email))


def _serialize_source_suggestion(suggestion: SourceSuggestion | None) -> dict | None:
    if suggestion is None:
        return None
    return {"value": suggestion.value, "matched_phrase": suggestion.matched_phrase}


def _serialize_notes_suggestion(suggestion: NotesSuggestion | None) -> dict | None:
    if suggestion is None:
        return None
    return {"value": suggestion.text}


def _compute_date_suggestions(document: Document) -> dict | None:
    """Locally suggest Document Date / Date Received values for `document`
    from its extracted (native-or-OCR) text, or None.

    Only offered while at least one of the two fields is still unset --
    mirrors `_compute_type_suggestion`'s "don't second-guess an
    already-answered field" gate. Once OCR completes for a scanned
    document, this is what lets the deferred suggestion surface (which
    already shows a document-type suggestion, FERChronos Step 5.6) also
    offer dates -- same `suggest_document_dates` call the pre-ingestion
    preview endpoint uses, just fed OCR-inclusive `effective_text()`
    instead of native-only text.
    """
    if document.document_date is not None and document.date_received is not None:
        return None

    text_sample = "\n".join(
        text
        for text in (
            effective_text(page).text
            for page in sorted(document.pages, key=lambda p: p.page_number)[:3]
        )
        if text
    )
    if not text_sample:
        return None

    is_email = Path(document.original_filename).suffix.lower() == ".eml"
    suggestions = _resolve_date_suggestions(text_sample, is_email=is_email)
    if document.document_date is not None:
        suggestions["document_date"] = None
    if document.date_received is not None:
        suggestions["date_received"] = None

    if not suggestions["document_date"] and not suggestions["date_received"] and not suggestions["informational_dates"]:
        return None
    return suggestions


def _native_text_sample(original_filename: str, temp_path: Path) -> str:
    """Best-effort native (never OCR) text sample for a not-yet-ingested
    file, for the pre-ingestion preview endpoint only -- OCR is a
    background job (see app/jobs/worker.py) and deliberately never runs
    synchronously in a request. An image file has no extractor at all
    (`get_extractor` returns None) and always needs OCR, so preview falls
    back to filename-only matching for it, same as any other case with no
    usable native text; a scanned/image document's type and date
    suggestions become available later, once OCR completes, via the
    existing deferred-suggestion display on the document detail page.
    Never raises -- a malformed/unparseable file simply yields no text
    sample rather than failing the preview.
    """
    if is_image_extension(original_filename):
        return ""
    extractor = get_extractor(original_filename)
    if extractor is None:
        return ""
    try:
        result = extractor(temp_path)
    except Exception:
        return ""
    return "\n".join(page.text or "" for page in result.pages[:3])


@router.get("/documents/{document_id}", response_class=HTMLResponse)
def get_document(
    request: Request, document_id: int, date_scan: int | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render a document's detail page: metadata, custody log, and version history."""
    document = _get_document_or_404(db, document_id)

    version_siblings = []
    if document.version_group_id is not None:
        version_siblings = sorted(
            db.scalars(
                select(Document).where(Document.version_group_id == document.version_group_id)
            ).all(),
            key=lambda d: d.version_number or 0,
        )

    other_case_documents = sorted(
        (
            d
            for d in document.case.documents
            if d.document_id != document.document_id
            and d.is_current_version
            and d.version_group_id != document.version_group_id
        ),
        key=lambda d: d.original_filename.lower(),
    )

    document_tags = sorted((link.tag for link in document.tag_links), key=lambda t: t.name.lower())
    case_tags = list_case_tags(db, document.case_id)
    annotation_counts = count_document_annotations(db, document.document_id)
    document_types = list(
        db.scalars(select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name)).all()
    )
    type_suggestion = _compute_type_suggestion(document, document_types)
    date_suggestions = _compute_date_suggestions(document)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "document_detail.html",
        {
            "document": document,
            "custody_events": document.custody_events,
            "version_siblings": version_siblings,
            "other_case_documents": other_case_documents,
            "pages": sorted(document.pages, key=lambda p: p.page_number),
            "document_tags": document_tags,
            "case_tags": case_tags,
            "annotation_counts": annotation_counts,
            "date_scan_result": date_scan,
            "document_types": document_types,
            "type_suggestion": type_suggestion,
            "date_suggestions": date_suggestions,
        },
    )


@router.post("/documents/{document_id}/document-type")
def set_document_type(
    document_id: int,
    document_type_id: str = Form(""),
    suggestion_source: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Set, change, or clear a document's type after ingestion.

    Covers both accepting/overriding a locally computed type suggestion
    (FERChronos Step 5.6) and simply correcting or filling in a type
    later -- the Document Type field is always editable, not just at
    upload time. Never touches the stored original file, its hash, or
    any other document field; only `document_type_id` changes, and only
    if it actually differs from the current value, logged as a custody
    event either way that records whether the final choice came from a
    suggestion or was picked manually -- see the Document model and
    docs/DATA_MODEL.md "document_custody_events". A suggestion is never
    applied automatically -- this route only ever runs in response to an
    explicit human POST.
    """
    document = _get_document_or_404(db, document_id)

    new_type_id = int(document_type_id) if document_type_id.strip() else None
    if new_type_id is not None and db.get(DocumentType, new_type_id) is None:
        raise HTTPException(status_code=400, detail=f"Document type {new_type_id} not found.")

    if new_type_id != document.document_type_id:
        previous_type_id = document.document_type_id
        document.document_type_id = new_type_id
        write_custody_event(
            db,
            document,
            event_type="document_type_set",
            actor=actor,
            details={
                "previous_document_type_id": previous_type_id,
                "new_document_type_id": new_type_id,
                "source": "suggested" if suggestion_source.strip() == "suggested" else "manual",
            },
        )
        db.commit()

    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.post("/documents/{document_id}/document-date")
def set_document_date_route(
    document_id: int,
    document_date: str = Form(""),
    document_date_precision: str = Form("exact"),
    document_date_range_end: str = Form(""),
    date_received: str = Form(""),
    document_date_source: str = Form(""),
    date_received_source: str = Form(""),
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Set, correct, or clear a document's own date and/or date received
    after ingestion.

    Covers both accepting/overriding a deferred date suggestion (see
    `_compute_date_suggestions`, offered once OCR text is available for a
    document with no date yet — FERChronos document drop-zone auto-fill)
    and simply correcting a date entered at ingestion time — mirrors
    `set_document_type` above: always editable, never applied
    automatically, and every actual change is logged to the custody log
    recording whether it came from a suggestion or was entered manually.
    A field that didn't actually change (same value resubmitted) writes no
    event.
    """
    document = _get_document_or_404(db, document_id)

    parsed_date, date_precision, parsed_range_end = _parse_document_date_form(
        document_date, document_date_precision, document_date_range_end
    )
    parsed_received = _parse_optional_date(date_received, "date received")

    current_date = document.document_date.date() if document.document_date else None
    current_range_end = (
        document.document_date_range_end.date() if document.document_date_range_end else None
    )
    current_received = document.date_received.date() if document.date_received else None

    changes: dict[str, dict] = {}

    if (
        parsed_date != current_date
        or parsed_range_end != current_range_end
        or (parsed_date is not None and date_precision.value != document.document_date_precision)
    ):
        source = (
            DocumentDateSource.EXTRACTED
            if document_date_source.strip() == "suggested"
            else DocumentDateSource.MANUAL
        )
        try:
            set_document_date(
                document, parsed_date, source=source, precision=date_precision, range_end=parsed_range_end
            )
        except InvalidDateRangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        changes["document_date"] = {
            "new_value": parsed_date.isoformat() if parsed_date else None,
            "source": "suggested" if document_date_source.strip() == "suggested" else "manual",
        }

    if parsed_received != current_received:
        document.date_received = (
            datetime.combine(parsed_received, time.min, tzinfo=timezone.utc)
            if parsed_received is not None
            else None
        )
        changes["date_received"] = {
            "new_value": parsed_received.isoformat() if parsed_received else None,
            "source": "suggested" if date_received_source.strip() == "suggested" else "manual",
        }

    if changes:
        write_custody_event(db, document, event_type="document_date_set", actor=actor, details=changes)
        db.commit()

    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.get("/documents/{document_id}/file")
def download_document_file(
    document_id: int,
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
):
    """Serve the stored original file, read-only, for viewing/downloading."""
    document = _get_document_or_404(db, document_id)
    full_path = vault.root / document.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Stored file is missing from the vault.")
    return FileResponse(
        path=full_path,
        filename=document.original_filename,
        media_type=document.mime_type or "application/octet-stream",
    )


@router.post("/documents/{document_id}/verify")
def verify_document(
    document_id: int,
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Re-verify a document's stored file against its recorded SHA-256 hash.

    Always logs a `hash_verified` custody event (see
    app/core/custody.py::verify_document_integrity) whether or not the hash
    still matches, so the result is visible in the custody log either way.
    """
    document = _get_document_or_404(db, document_id)
    verify_document_integrity(db, vault, document, actor)
    db.commit()
    return RedirectResponse(url=f"/documents/{document.document_id}", status_code=303)


@router.post("/documents/{document_id}/new-version")
def upload_new_version(
    request: Request,
    document_id: int,
    file: UploadFile,
    version_note: str = Form(""),
    document_date: str = Form(""),
    document_date_precision: str = Form("exact"),
    document_date_range_end: str = Form(""),
    date_received: str = Form(""),
    document_date_source: str = Form(""),
    date_received_source: str = Form(""),
    db: Session = Depends(get_db),
    vault: VaultLayout = Depends(get_vault),
    actor: str = Depends(get_actor),
) -> RedirectResponse:
    """Ingest an uploaded file and link it as the new current version of `document_id`.

    Combines ingestion and version-linking into one step: the existing
    document is never modified, and the new file becomes its own
    independent document row before being linked — see
    app/core/ingestion/versioning.py.

    The new version's document date (and date received) is entered fresh
    here, not copied from the prior version: a reissued or corrected record
    often carries a new effective date (e.g. the date it was reissued) and
    is typically received on its own new date too, so silently inheriting
    either from the old version could record the wrong date rather than an
    honestly "unknown" one.
    """
    existing_document = _get_document_or_404(db, document_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    parsed_date, date_precision, parsed_range_end = _parse_document_date_form(
        document_date, document_date_precision, document_date_range_end
    )
    parsed_received = _parse_optional_date(date_received, "date received")
    field_provenance = _collect_field_provenance(
        document_date=document_date_source,
        date_received=date_received_source,
    )

    temp_path = _save_upload_to_temp(file)
    try:
        try:
            new_document = ingest_document(
                db,
                vault,
                existing_document.case,
                source_file_path=temp_path,
                original_filename=file.filename,
                actor=actor,
                source=existing_document.source,
                document_type_id=existing_document.document_type_id,
                mime_type=file.content_type,
                document_date=parsed_date,
                document_date_precision=date_precision,
                document_date_range_end=parsed_range_end,
                date_received=parsed_received,
                field_provenance=field_provenance,
            )
        except DuplicateDocumentError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidDateRangeError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        extract_document(db, vault, new_document, actor)
        _maybe_enqueue_ocr(db, new_document, actor)

        try:
            link_as_new_version(
                db,
                existing_document=existing_document,
                new_document=new_document,
                actor=actor,
                version_note=version_note.strip() or None,
            )
        except VersionLinkError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        db.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return RedirectResponse(url=f"/documents/{new_document.document_id}", status_code=303)
