"""Evidence validation and an append-only decision log inside each private upload."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import file_digest, sha256
from pathlib import Path
from secrets import token_hex

import pymupdf

from .batch_evidence import EvidenceError, normalize
from .models import ResultRecord, UploadProcessing
from .review_models import (
    ReviewBasis, ReviewDecision, ReviewDetail, ReviewedResult, ReviewEvent, ReviewOverview, ReviewState,
)
from .upload_processing import UploadProcessingStore
from .upload_store import mupdf_lock, private_sqlite_path

APPROVED = {"approved", "approved_corrected"}
ACTIVE = {"queued", "processing"}
DECISIONS = {"approve": "approved", "propose_correction": "correction_proposed",
             "approve_correction": "approved_corrected", "reject": "rejected"}


class ReviewError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def source_result_hash(result: ResultRecord) -> str:
    snapshot = json.dumps(result.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256(snapshot.encode("utf-8")).hexdigest()


class ReviewLog:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def _connect(self, *, write=False):
        path = private_sqlite_path(self.store.root, Path("reviews.sqlite3"))
        if not write and not path.is_file():
            yield None
            return
        with closing(sqlite3.connect(path, timeout=15)) as db, db:
            if write:
                db.execute("""CREATE TABLE IF NOT EXISTS review_events (
                    result_id TEXT NOT NULL, revision INTEGER NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY (result_id, revision)
                )""")
                db.execute("BEGIN IMMEDIATE")
            elif not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'review_events'").fetchone():
                yield None
                return
            yield db

    @staticmethod
    def _latest(db, result_id):
        row = db.execute("""SELECT event FROM review_events WHERE result_id = ?
            ORDER BY revision DESC LIMIT 1""", (result_id,)).fetchone()
        return ReviewEvent.model_validate_json(row[0]) if row else None

    def latest(self, result_id):
        with self._connect() as db:
            return self._latest(db, result_id) if db else None

    def current(self):
        with self._connect() as db:
            if db is None:
                return []
            rows = db.execute("""SELECT event FROM review_events JOIN (
                    SELECT result_id, MAX(revision) AS revision FROM review_events GROUP BY result_id
                ) USING (result_id, revision) ORDER BY result_id""")
            return [ReviewEvent.model_validate_json(row[0]) for row in rows]

    def history(self, result_id):
        with self._connect() as db:
            if db is None:
                return []
            rows = db.execute("SELECT event FROM review_events WHERE result_id = ? ORDER BY revision", (result_id,))
            return [ReviewEvent.model_validate_json(row[0]) for row in rows]

    def all_events(self, max_bytes):
        with self._connect() as db:
            if db is None:
                return []
            # Read the size and the events from one SQLite read snapshot.
            db.execute("BEGIN")
            size = db.execute("SELECT COALESCE(SUM(length(CAST(event AS BLOB))), 0) FROM review_events").fetchone()[0]
            if size > max_bytes:
                raise ReviewError(413, "upload_export_too_large")
            rows = db.execute("SELECT result_id, revision, event FROM review_events ORDER BY result_id, revision")
            events = []
            for result_id, revision, raw in rows:
                event = ReviewEvent.model_validate_json(raw)
                if event.revision != revision:
                    raise ValueError("Review event revision mismatch")
                events.append((result_id, event))
            return events

    @contextmanager
    def transaction(self, result_id, *, actor_user_id=None):
        with self._connect(write=True) as db:
            if actor_user_id is not None:
                db.execute("""CREATE TABLE IF NOT EXISTS review_actors (
                    result_id TEXT NOT NULL, revision INTEGER NOT NULL, actor_user_id TEXT NOT NULL,
                    PRIMARY KEY (result_id, revision),
                    FOREIGN KEY (result_id, revision) REFERENCES review_events (result_id, revision)
                )""")

            def append(event):
                db.execute("INSERT INTO review_events VALUES (?, ?, ?)",
                           (result_id, event.revision, event.model_dump_json()))
                if actor_user_id is not None:
                    db.execute("INSERT INTO review_actors VALUES (?, ?, ?)",
                               (result_id, event.revision, actor_user_id))

            yield self._latest(db, result_id), append


def _decimal(value):
    try:
        number = Decimal(str(value))
        if number.is_finite():
            return number
    except InvalidOperation:
        pass
    raise ReviewError(422, "invalid_normalized_value")


def _validate_normalization(original, result, explanation, *, verify_unchanged=False):
    normalized = result.normalized
    if normalized.value is None:
        if normalized.unit is not None:
            raise ReviewError(422, "invalid_normalized_value")
        return
    protocol_keys = ("evaluation_protocol", "metric_definition")
    unchanged = (all(getattr(original, field) == getattr(result, field)
                     for field in ("normalized", "raw", "metric", "source"))
                 and all(original.context.get(key) == result.context.get(key) for key in protocol_keys))
    if unchanged and not verify_unchanged:
        return
    value = _decimal(normalized.value)
    try:
        expected = normalize(str(result.raw.value), result.raw.unit or "", result.metric.name, result.domain)
    except EvidenceError:
        expected = None
    if expected is not None and (expected.unit != normalized.unit or _decimal(expected.value) != value):
        raise ReviewError(422, "inconsistent_normalization")
    if unchanged:
        return
    protocol = result.domain == "rag" and any(
        isinstance(result.context.get(key), str) and result.context[key].strip()
        for key in protocol_keys
    )
    if not (((result.raw.unit and normalized.unit) or protocol) and explanation and explanation.strip()):
        raise ReviewError(422, "normalization_evidence_required")


def _validate_origin(result):
    source = result.source
    if (source.origin == "cited") != bool(source.cited_publication_id and source.cited_publication_id.strip()):
        raise ReviewError(422, "invalid_review_origin")
    if result.domain == "rag" and source.origin is None:
        raise ReviewError(422, "review_origin_required")


def validate_reviewed_record(original, result, normalization_evidence, *, verify_unchanged=False):
    if isinstance(result.raw.value, str) and not result.raw.value.strip():
        raise ReviewError(422, "invalid_review_correction")
    if not result.metric.name.strip() or any(unit is not None and not unit.strip()
                                            for unit in (result.raw.unit, result.normalized.unit)):
        raise ReviewError(422, "invalid_review_correction")
    if "metric_k" in result.context:
        raise ReviewError(422, "invalid_review_context")
    json.dumps(result.context, allow_nan=False)
    if result.domain == "rag" and (top_k := result.context.get("top_k")) is not None:
        if not ((type(top_k) is int and top_k >= 0)
                or (isinstance(top_k, str) and top_k.isascii() and top_k.isdecimal())):
            raise ReviewError(422, "invalid_review_context")
    _validate_origin(result)
    _validate_normalization(original, result, normalization_evidence, verify_unchanged=verify_unchanged)


def _correct(original, correction):
    data = original.model_dump()
    changes = {key: value for key, value in correction.model_dump(exclude={"normalization_evidence"}).items()
               if value is not None}
    if "metric" in changes:
        changes["metric"] = {**data["metric"], **changes["metric"]}
    if "context" in changes:
        changes["context"] = {**data["context"], **changes["context"]}
    context = changes.get("context", data["context"])
    if "normalized" in changes and "unit_status" in context:
        changes["context"] = {**context, "unit_status": "confirmed" if changes["normalized"]["value"] is not None
                              else "unconfirmed"}
    data.update(changes)
    result = ResultRecord.model_validate(data)
    if result == original:
        raise ReviewError(422, "empty_review_correction")
    validate_reviewed_record(original, result, correction.normalization_evidence)
    return result


class UploadReviews:
    """One stable queue snapshot for a read or a complete review transaction."""

    def __init__(self, uploads, upload, root, job, *, write=False, verified_pdf=False):
        if upload.owner_user_id != uploads.owner_user_id:
            raise ReviewError(404, "upload_not_found")
        self.upload = upload
        self.processing = job.processing if job else UploadProcessing(upload_id=upload.upload_id, status="not_started")
        self.store = UploadProcessingStore(root, upload)
        self.log = ReviewLog(self.store)
        self.results = {}
        self.issues = []
        if job and job.matches(upload) and job.processing.counts is not None:
            checkpoint = self.store.checkpoint()
            if checkpoint is None:
                raise ValueError("Missing private checkpoint for a finished job")
            self.results = {result.result_id: result for result in self.store.upload_results()}
            self.issues = self.store.upload_issues(checkpoint)[0]
        self.pdf_content = None
        if verified_pdf:
            self.pdf_status = "valid"
            return
        self.pdf_status = "missing"
        path = uploads.file(upload)
        if path is not None:
            try:
                with path.open("rb") as stream:
                    if write:
                        self.pdf_content = stream.read(upload.size_bytes + 1)
                        digest = sha256(self.pdf_content).hexdigest()
                    else:
                        digest = file_digest(stream, "sha256").hexdigest()
                self.pdf_status = "valid" if digest == upload.sha256 else "mismatch"
            except FileNotFoundError:
                pass

    def _decision(self, event, result_id):
        if event is None:
            return None
        decision = event.decision
        expected = (self.upload.upload_id, self.upload.domain, result_id)
        if (decision.upload_id, decision.domain, decision.result_id) != expected:
            raise ValueError("Review event identity mismatch")
        return decision

    def _state(self, result_id, event):
        original = self.results.get(result_id)
        decision = self._decision(event, result_id)
        if original is None and decision is None:
            raise ReviewError(404, "review_result_not_found")
        basis = ReviewBasis(
            upload_id=self.upload.upload_id, domain=self.upload.domain, result_id=result_id,
            source_job_id=self.processing.job_id, pdf_sha256=self.upload.sha256,
            source_result_hash=source_result_hash(original),
        ) if original else None
        applicable = bool(decision and basis and self.pdf_status == "valid"
                          and basis.model_dump() == decision.model_dump(include=set(ReviewBasis.model_fields)))
        status = decision.status if applicable else "stale" if decision else "pending"
        return ReviewState(result_id=result_id, original_result=original, basis=basis, status=status,
                           revision=decision.revision if decision else 0, applicable=applicable, decision=decision)

    def states(self, events=None):
        if events is None:
            events = {event.decision.result_id: event for event in self.log.current()}
        return [self._state(result_id, events.get(result_id)) for result_id in {**self.results, **events}]

    def _warnings(self, result_id, pages):
        return [issue for issue in self.issues if result_id in issue.result_ids or (
            not issue.result_ids and any(
                ref.get("page") in pages if isinstance(ref.get("page"), int)
                else ref.get("page") is None and ref.get("document_id") == self.store.document_id
                for ref in issue.evidence_refs
            )
        )]

    def _detail(self, state):
        pages = {state.original_result.source.page} if state.original_result else set()
        if state.applicable and state.decision.reviewed_result:
            pages.add(state.decision.reviewed_result.source.page)
        return ReviewDetail(**state.model_dump(), processing_status=self.processing.status, pdf_status=self.pdf_status,
                            issues=self._warnings(state.result_id, pages), ocr_pages=self.processing.pages_needing_ocr)

    def overview(self):
        return ReviewOverview(processing=self.processing, pdf_status=self.pdf_status,
                              reviews=self.states(), issues=self.issues)

    def detail(self, result_id):
        return self._detail(self._state(result_id, self.log.latest(result_id)))

    def history(self, result_id):
        events = self.log.history(result_id)
        self._state(result_id, events[-1] if events else None)
        for event in events:
            self._decision(event, result_id)
        return events

    def reviewed_results(self, states=None):
        if states is None:
            states = self.states()
        return [ReviewedResult(original_result=state.original_result,
                               reviewed_result=state.decision.reviewed_result or state.original_result,
                               decision=state.decision, issues=self._detail(state).issues,
                               ocr_pages=self.processing.pages_needing_ocr)
                for state in states if state.status in APPROVED]

    def save(self, result_id, request):
        if self.processing.status in ACTIVE:
            raise ReviewError(409, "review_processing_active")
        if result_id not in self.results:
            self._state(result_id, self.log.latest(result_id))
            raise ReviewError(409, "review_source_changed")
        if self.pdf_status != "valid":
            raise ReviewError(409, "review_pdf_changed")

        with self.log.transaction(result_id, actor_user_id=self.upload.owner_user_id) as (previous, append):
            state = self._state(result_id, previous)
            if state.basis != request.basis:
                raise ReviewError(409, "review_source_changed")
            if state.revision != request.expected_revision:
                raise ReviewError(409, "review_revision_conflict")
            decision = self._decide(state, request)
            result = decision.reviewed_result or state.original_result
            pages = {state.original_result.source.page, result.source.page}
            warnings = self._warnings(result_id, pages)
            if decision.status in APPROVED and warnings and not request.warnings_acknowledged:
                raise ReviewError(422, "review_warnings_unacknowledged")
            append(ReviewEvent(
                action=request.action, previous_revision=state.revision, revision=decision.revision,
                original_result=state.original_result, previous_decision=state.decision, decision=decision,
                correction=request.correction, issues=warnings,
                ocr_pages=sorted(pages.intersection(self.processing.pages_needing_ocr)),
            ))
            return self._detail(ReviewState(
                result_id=result_id, original_result=state.original_result, basis=state.basis,
                status=decision.status, revision=decision.revision, applicable=True, decision=decision,
            ))

    def _decide(self, state, request):
        result = None
        normalization_evidence = None
        if request.action == "propose_correction":
            try:
                result = _correct(state.original_result, request.correction)
            except ValueError as exc:
                raise ReviewError(422, "invalid_review_correction") from exc
            normalization_evidence = request.correction.normalization_evidence
        elif request.action == "approve_correction":
            if state.status != "correction_proposed":
                raise ReviewError(409, "review_proposal_required")
            result = state.decision.reviewed_result
            normalization_evidence = state.decision.normalization_evidence

        status = DECISIONS[request.action]
        evidence_result = result or state.original_result
        if status in APPROVED:
            _validate_origin(evidence_result)
        self._check_page(evidence_result.source.page, request.evidence_mode)
        return ReviewDecision(
            **state.basis.model_dump(), review_id=token_hex(16), revision=state.revision + 1, status=status,
            reviewed_at=datetime.now(timezone.utc), reviewer_label=request.reviewer_label, reason=request.reason,
            evidence_mode=request.evidence_mode, warnings_acknowledged=request.warnings_acknowledged,
            reviewed_result=result, normalization_evidence=normalization_evidence,
        )

    def _check_page(self, page, evidence_mode):
        with mupdf_lock, pymupdf.open(stream=self.pdf_content, filetype="pdf") as pdf:
            if pdf.page_count != self.upload.page_count:
                raise ReviewError(409, "review_pdf_changed")
            if not 1 <= page <= pdf.page_count:
                raise ReviewError(422, "review_page_outside_pdf")
            if evidence_mode == "text" and (page in self.processing.pages_needing_ocr
                                            or not pdf[page - 1].get_text().strip()):
                raise ReviewError(422, "review_visual_evidence_required")
