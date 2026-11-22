"""Read-only snapshots and artifacts for a private scientific project."""

import csv
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from io import StringIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from . import __version__
from .pdf_boundary import ParserUnavailable
from .review_models import ReviewBasis
from .upload_export_models import (
    CSV_FORMAT, DEFAULT_EXPORT_BYTES, MANIFEST, MEMBERS, ROOT, AuditEvent, CurrentReview, ExportArtifact,
    ExportManifest, ExportWarning, FileChecksum, FrozenSnapshot, IssueCounts, IssuesDocument, ManifestPreview,
    PackageMetadata, ReviewCounts, ReviewedResultsDocument, ReviewHistoryDocument, SnapshotData, ValidationSummary,
)
from .upload_processing import UploadProcessingStore
from .upload_reviews import ACTIVE, APPROVED, ReviewError, UploadReviews, source_result_hash, validate_reviewed_record
from .upload_store import InvalidPdf, _pdf_pages, _positive_limit

DEFAULT_SNAPSHOT_BYTES = 16 * 1024 * 1024
CSV_COLUMNS = (
    "upload_id", "domain", "document_id", "result_id", "review_status", "review_revision", "review_id",
    "source_job_id", "pdf_sha256", "source_result_hash", "metric_name", "metric_k", "metric_variant",
    "raw_value", "raw_unit", "normalized_value", "normalized_unit", "source_page", "source_section",
    "source_table", "source_figure", "source_fragment", "source_origin", "cited_publication_id",
    "context_json", "reviewer_label", "reviewed_at", "review_reason", "evidence_mode", "warnings_acknowledged",
    "source_row", "source_column",
)
NEGATIVE_NUMBER = re.compile(r"-(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
ARTIFACTS = {
    "reviewed_results.csv": "text/csv; charset=utf-8",
    "reviewed_results.json": "application/json",
    "scientific-passport.pdf": "application/pdf",
}


def _json(model, *, newline=True):
    value = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
    return (value + ("\n" if newline else "")).encode("utf-8")


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _verified_original(uploads, upload):
    path = uploads.file(upload)
    if path is None:
        raise ReviewError(409, "upload_export_pdf_changed")
    try:
        path_stat = path.stat()
        with path.open("rb") as stream:
            original = os.fstat(stream.fileno())
            # On Windows, stat() and fstat() can report different ctime
            # semantics. Compare it only against the same stat operation.
            if (_signature(path_stat)[:-1] != _signature(original)[:-1] or original.st_size != upload.size_bytes
                    or file_digest(stream, "sha256").hexdigest() != upload.sha256):
                raise ReviewError(409, "upload_export_pdf_changed")
            if _pdf_pages(path) != upload.page_count or _signature(os.fstat(stream.fileno())) != _signature(original):
                raise ReviewError(409, "upload_export_pdf_changed")
    except ParserUnavailable:
        raise
    except (OSError, InvalidPdf) as exc:
        raise ReviewError(409, "upload_export_pdf_changed") from exc
    return path, _signature(path_stat)


def _require_original_signature(path, signature):
    try:
        if _signature(path.stat()) == signature:
            return
    except OSError:
        pass
    raise ReviewError(409, "upload_export_pdf_changed")


def _eligible_job(job, upload):
    if job and job.processing.status in ACTIVE:
        raise ReviewError(409, "upload_export_processing_active")
    if job is None or job.processing.counts is None or job.processing.status not in {"completed", "needs_review", "failed"}:
        raise ReviewError(409, "upload_export_analysis_unavailable")
    if not job.matches(upload):
        raise ReviewError(409, "upload_export_source_changed")


def _read_sources(uploads, upload, queue):
    initial_job = queue.get(upload.upload_id)
    _eligible_job(initial_job, upload)
    # Stream the original and check its PDF structure before reserving the queue.
    path, signature = _verified_original(uploads, upload)
    max_bytes = _positive_limit("SPI_UPLOAD_EXPORT_MAX_SNAPSHOT_BYTES", DEFAULT_SNAPSHOT_BYTES)
    with queue.review_snapshot(upload.upload_id, write=True) as job:
        if job != initial_job:
            raise ReviewError(409, "upload_export_source_changed")
        _require_original_signature(path, signature)
        store = UploadProcessingStore(queue.root, upload)
        remaining = max_bytes - store.export_source_bytes()
        if remaining < 0:
            raise ReviewError(413, "upload_export_too_large")
        reviews = UploadReviews(uploads, upload, queue.root, job, verified_pdf=True)
        events = reviews.log.all_events(remaining)
        _require_original_signature(path, signature)
        captured_at = datetime.now(timezone.utc)
    return reviews, events, captured_at


def _basis(decision):
    return decision.model_dump(include=set(ReviewBasis.model_fields))


def _validate_approved(reviews, rows):
    upload = reviews.upload
    document_id = reviews.store.document_id
    for row in rows:
        original, result, decision = row.original_result, row.reviewed_result, row.decision
        if (original.document_id != document_id or result.document_id != document_id
                or original.domain != upload.domain or result.domain != upload.domain
                or original.extraction_metadata is None or original.extraction_metadata.pdf_sha256 != upload.sha256
                or (result.result_id, result.extraction_status, result.extraction_metadata)
                != (original.result_id, original.extraction_status, original.extraction_metadata)):
            raise ReviewError(409, "upload_export_source_changed")
        if ((decision.status == "approved" and result != original)
                or (decision.status == "approved_corrected" and result == original)
                or not 1 <= result.source.page <= upload.page_count
                or not 1 <= original.source.page <= upload.page_count
                or (decision.evidence_mode == "text" and result.source.page in row.ocr_pages)):
            raise ReviewError(409, "upload_export_invalid_review")
        if not any(value and value.strip() for value in (
            result.source.fragment, result.source.table, result.source.figure, result.source.section,
        )):
            raise ReviewError(409, "upload_export_invalid_review")
        try:
            validate_reviewed_record(original, result, decision.normalization_evidence, verify_unchanged=True)
        except (ReviewError, ValueError) as exc:
            raise ReviewError(409, "upload_export_invalid_review") from exc


def _snapshot_data(reviews, events):
    latest = {}
    for result_id, event in events:
        decision, original = event.decision, event.original_result
        previous = latest.get(result_id)
        if ((decision.upload_id, decision.domain, decision.result_id)
                != (reviews.upload.upload_id, reviews.upload.domain, result_id)
                or (original.result_id, original.document_id) != (result_id, reviews.store.document_id)
                or source_result_hash(original) != decision.source_result_hash
                or event.previous_decision != (previous.decision if previous else None)):
            raise ReviewError(409, "upload_export_source_changed")
        latest[result_id] = event
    states = sorted(reviews.states(latest), key=lambda state: state.result_id)
    bases = {state.result_id: state.basis.model_dump() for state in states if state.basis is not None}
    audited = tuple(AuditEvent(
        applicability=("stale" if bases.get(result_id) != _basis(event.decision)
                       else "current" if event is latest[result_id] else "superseded"),
        event=event,
    ) for result_id, event in events)
    rows = tuple(reviews.reviewed_results(states))
    _validate_approved(reviews, rows)
    automatic = tuple(reviews.results[key] for key in sorted(reviews.results))
    issues = tuple(sorted(reviews.issues, key=lambda issue: issue.issue_id))
    if (len(automatic), len(issues)) != (reviews.processing.counts.results, reviews.processing.counts.issues):
        raise ReviewError(409, "upload_export_source_changed")
    return SnapshotData(
        processing=reviews.processing, automatic_results=automatic, reviewed_results=rows, issues=issues, events=audited,
        current_reviews=tuple(CurrentReview(
            result_id=state.result_id, status=state.status, revision=state.revision,
            review_id=state.decision.review_id if state.decision else None,
        ) for state in states),
    )


def _summary(data):
    states = Counter(row.status for row in data.current_reviews)
    severities = Counter(issue.severity for issue in data.issues)
    automatic_ids = {row.result_id for row in data.automatic_results}
    approved = len(data.reviewed_results)
    processing = data.processing
    ocr = tuple(sorted(set(processing.pages_needing_ocr)))
    warnings = [
        ExportWarning(code="scientific_completeness_unverified",
                      message="Human decisions cover individual records. Processing status describes the technical attempt."),
        ExportWarning(code="private_not_official_submission", message="Private project from the configured upload store. "
                      "This package has no official submission status."),
    ]
    if not approved:
        if automatic_ids:
            message = "Candidates exist. Open their PDF evidence and approve results to populate the reviewed CSV and JSON."
        elif processing.pages_needing_ocr:
            message = "No candidates have been extracted. Some pages require OCR."
        elif processing.status == "failed" or processing.counts.pending_fragments:
            message = "No candidates have been extracted. Processing is incomplete."
        elif processing.coverage == "present_not_extractable":
            message = "No candidates have been extracted. See unresolved source evidence in issues.json."
        else:
            message = "No supported scientific values were extracted from the available evidence."
        warnings.append(ExportWarning(code="zero_approved_results", message=message))
    if ocr:
        warnings.append(ExportWarning(code="incomplete_ocr", message="Some PDF pages still require OCR."))
    if processing.mode == "local":
        warnings.append(ExportWarning(code="local_only", message="Local extraction only. No model extraction was run."))
    counts = processing.counts
    if (processing.status == "failed" or counts.pending_fragments or counts.unresolved
            or counts.pages_analyzed < counts.pages
            or processing.coverage in {"partial_table_only", "pdf_pages_unprocessed", "model_fragments_pending",
                                       "present_not_extractable"}):
        warnings.append(ExportWarning(code="partial_analysis", message="Analysis coverage is incomplete. "
                      "See processing details and scientific issues."))
    if severities["warning"] or severities["error"]:
        warnings.append(ExportWarning(code="unresolved_issues", message="Review approval does not close scientific issues."))
    return ValidationSummary(
        review_counts=ReviewCounts(
            automatic=len(automatic_ids), approved=states["approved"], approved_corrected=states["approved_corrected"],
            pending=states["pending"], correction_proposed=states["correction_proposed"],
            rejected=states["rejected"], stale=states["stale"],
            unconfirmed=sum(row.result_id in automatic_ids and row.status in {"pending", "correction_proposed", "stale"}
                            for row in data.current_reviews),
        ),
        issue_counts=IssueCounts(total=len(data.issues), info=severities["info"],
                                 warning=severities["warning"], error=severities["error"]),
        ocr_pending_pages=ocr, processing_mode=processing.mode, extraction_coverage=processing.coverage,
        user_review_status=("audit_only" if not approved else "all_current_results_approved"
                            if approved == len(automatic_ids) else "partially_approved"),
        warnings=tuple(warnings),
    )


def _snapshot_payload(data):
    # Linked issues can occur in many reviewed rows. Bound serialization as
    # each item is written, before materializing the combined JSON.
    maximum = _positive_limit("SPI_UPLOAD_EXPORT_MAX_SNAPSHOT_BYTES", DEFAULT_SNAPSHOT_BYTES)
    payload = bytearray()

    def append(chunk):
        if len(payload) + len(chunk) > maximum:
            raise ReviewError(413, "upload_export_too_large")
        payload.extend(chunk)

    append(b"{")
    for field_index, (field, value) in enumerate(sorted(data)):
        if field_index:
            append(b",")
        append(json.dumps(field).encode("ascii") + b":")
        if isinstance(value, tuple):
            append(b"[")
            for index, row in enumerate(value):
                if index:
                    append(b",")
                append(_json(row, newline=False))
            append(b"]")
        else:
            append(_json(value, newline=False))
    append(b"}")
    return bytes(payload)


def capture_snapshot(uploads, upload, queue, *, expected=None):
    reviews, events, captured_at = _read_sources(uploads, upload, queue)
    data = _snapshot_data(reviews, events)
    payload = _snapshot_payload(data)
    fingerprint = sha256(upload.model_dump_json().encode("utf-8") + b"\n" + payload).hexdigest()
    if expected is not None and expected != fingerprint:
        raise ReviewError(409, "upload_export_source_changed")
    metadata = PackageMetadata(
        upload_id=upload.upload_id, domain=upload.domain, pdf_sha256=upload.sha256,
        original_filename=upload.original_filename, processing_job_id=data.processing.job_id,
        processing_status=data.processing.status, exported_at=captured_at, application_version=__version__,
        snapshot_sha256=fingerprint,
    )
    return FrozenSnapshot(payload=payload, preview=ManifestPreview(metadata=metadata, validation=_summary(data)))


def _csv_cell(value, *, scientific_value=False):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        return value
    if scientific_value and NEGATIVE_NUMBER.fullmatch(value):
        return value
    if value.startswith("'"):
        return "'" + value
    for char in value:
        if char in "\t\r\n":
            return "'" + value
        if char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        if unicodedata.normalize("NFKC", char) in {"=", "+", "-", "@"}:
            return "'" + value
        break
    return value


def _csv(rows):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        result, review = row.reviewed_result, row.decision
        metric, source = result.metric, result.source
        values = (
            review.upload_id, result.domain, result.document_id, result.result_id, review.status, review.revision,
            review.review_id, review.source_job_id, review.pdf_sha256, review.source_result_hash,
            metric.name, metric.k, metric.variant, result.raw.value, result.raw.unit,
            result.normalized.value, result.normalized.unit, source.page, source.section, source.table, source.figure,
            source.fragment, source.origin, source.cited_publication_id,
            json.dumps(result.context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
            review.reviewer_label, review.reviewed_at.isoformat(), review.reason, review.evidence_mode,
            review.warnings_acknowledged, source.row, source.column,
        )
        writer.writerow(_csv_cell(value, scientific_value=column in {"raw_value", "normalized_value"})
                        for column, value in zip(CSV_COLUMNS, values, strict=True))
    return output.getvalue().encode("utf-8-sig")


def _content(name, data, preview):
    metadata = preview.metadata
    digest = metadata.snapshot_sha256
    if name == "reviewed_results.csv":
        return _csv(data.reviewed_results)
    if name == "reviewed_results.json":
        return _json(ReviewedResultsDocument(snapshot_sha256=digest, results=data.reviewed_results))
    if name == "issues.json":
        return _json(IssuesDocument(snapshot_sha256=digest, issues=data.issues, processing=data.processing))
    if name == "review_history.json":
        return _json(ReviewHistoryDocument(snapshot_sha256=digest, automatic_results=data.automatic_results,
                                          current_reviews=data.current_reviews, events=data.events))
    if name == "scientific-passport.pdf":
        from .upload_export_passport import render_passport
        return render_passport(data, preview)
    if name == "README.md":
        return PACKAGE_README.encode("utf-8")
    raise ValueError("Unknown private export artifact")


def max_export_bytes():
    return _positive_limit("SPI_UPLOAD_EXPORT_MAX_BYTES", DEFAULT_EXPORT_BYTES)


def build_artifact(snapshot, name):
    data = SnapshotData.model_validate_json(snapshot.payload)
    content = _content(name, data, snapshot.preview)
    if len(content) > max_export_bytes():
        raise ReviewError(413, "upload_export_too_large")
    return ExportArtifact(filename=name, media_type=ARTIFACTS[name], content=content)


def build_archive(snapshot, output):
    data = SnapshotData.model_validate_json(snapshot.payload)
    maximum = max_export_bytes()
    expanded = 0
    checksums = []
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            content = _content(name, data, snapshot.preview)
            expanded += len(content)
            if expanded > maximum:
                raise ReviewError(413, "upload_export_too_large")
            checksums.append(FileChecksum(name=name, size_bytes=len(content), sha256=sha256(content).hexdigest()))
            archive.writestr(ZipInfo(ROOT + name), content, compress_type=ZIP_DEFLATED)
            del content
        manifest = _json(ExportManifest(metadata=snapshot.preview.metadata, validation=snapshot.preview.validation,
                                        files=tuple(checksums)))
        if expanded + len(manifest) > maximum:
            raise ReviewError(413, "upload_export_too_large")
        archive.writestr(ZipInfo(ROOT + MANIFEST), manifest, compress_type=ZIP_DEFLATED)
    size = output.tell()
    if size > maximum:
        raise ReviewError(413, "upload_export_too_large")
    output.seek(0)
    return size


def archive_filename(metadata):
    return f"spi-project-{metadata.domain}-{metadata.upload_id}.zip"


PACKAGE_README = f"""# Scientific PDF Intelligence | Private scientific project

This package contains confidential upload data. Share it only with authorized recipients.
The original PDF is available separately through the authenticated upload API.
This package has no official submission status.

| File | Contents |
| --- | --- |
| manifest.json | Captured upload and job, review counts, limitations, formats and file checksums |
| reviewed_results.csv | Current human-approved records in {CSV_FORMAT} |
| reviewed_results.json | Typed original and reviewed records, decisions, issues and OCR pages |
| issues.json | Current scientific issues, evidence references and page-level processing coverage |
| review_history.json | All current automatic results, current review states and full decision history |
| scientific-passport.pdf | Human-readable report for the same frozen snapshot |

The CSV is comma-delimited UTF-8 with BOM, with rows sorted by result ID. Empty cells represent
null or empty text. JSON is authoritative for types and exact source strings. CSV cells with
spreadsheet control characters or formula-like prefixes are escaped with an apostrophe.
Existing leading apostrophes are doubled. Plain negative scientific numbers remain numeric.
The metric_k column is the cutoff in the metric definition. Retrieval top_k is in context_json.

Automatic extraction describes the machine result. Approved and approved_corrected describe
current decisions made by a human, with the automatic record preserved for provenance.
Pending means no applicable decision. A correction proposal is awaiting approval. Rejected
means a human declined the record. Stale means a decision refers to a different job, PDF or
result version. Historical decisions retain their original status. A superseded event still
refers to the current source but a newer decision replaced it. The reviewer label is self-reported.

A completed job is a technical state. OCR gaps, analysis coverage and scientific issues
remain relevant after an individual approval. A zero_approved_results warning marks an audit-only export.
PDF text displays uncommon unsupported characters as Unicode escapes. The JSON holds the full Unicode text.

Run `python -m spi.upload_export_verify <archive.zip>` locally to check the fixed file list,
formats, byte sizes and SHA-256 digests. The manifest covers every other member and excludes
itself. SHA-256 checks integrity. Authenticity requires independent provenance.
"""
