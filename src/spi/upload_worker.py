"""Run private upload jobs in a separate process: python -m spi.upload_worker."""

import argparse
import logging
import os
import re
import sqlite3
from contextlib import ExitStack
from hashlib import sha256
from time import sleep

from .batch import run_batch
from .config import load_project_env
from .gigachat import GigaChatClient, GigaChatSettings
from .models import ProcessingCounts, ProcessingPage
from .upload_processing import UploadProcessingStore
from .upload_queue import UploadQueue
from .upload_store import UploadStore, upload_token

logger = logging.getLogger(__name__)


class ProcessingFailure(ValueError):
    """A diagnostic code suitable for the authenticated status endpoint."""


def _input(uploads, job):
    upload = uploads.get(job.processing.upload_id)
    if upload is None or not job.matches(upload):
        raise ProcessingFailure("upload_identity_mismatch")
    try:
        path = uploads.file(upload)
        if path is None:
            raise ProcessingFailure("original_unavailable")
        with path.open("rb") as source:
            content = source.read(upload.size_bytes + 1)
    except OSError as exc:
        raise ProcessingFailure("original_unavailable") from exc
    if len(content) != upload.size_bytes or sha256(content).hexdigest() != upload.sha256:
        raise ProcessingFailure("original_sha256_mismatch")
    return upload, path, content


def _finish(queue, job, store):
    record = store.checkpoint()
    if record is None or record["status"] not in {"processed", "partial"}:
        raise ProcessingFailure("batch_failed")
    if record["page_count"] != store.upload.page_count:
        raise ProcessingFailure("upload_page_count_mismatch")

    results = store.upload_results()
    issues, unresolved = store.upload_issues(record)
    pages = [ProcessingPage(page=p["page"], read_status=p["read_status"], ocr_status=p["ocr_status"],
                            analysis_failed=bool(p.get("analysis_error"))) for p in record["pages"]]
    ocr = record["pages_needing_ocr"]
    pending = record["pending_units"]
    needs_review = sum(row.extraction_status == "needs_review" for row in results)
    counts = ProcessingCounts(
        pages=len(pages), pages_analyzed=sum(p.read_status in {"text", "empty"} and not p.analysis_failed for p in pages),
        results=len(results), needs_review=needs_review, issues=len(issues), unresolved=unresolved,
        pending_fragments=pending,
    )
    diagnostic = job.processing.diagnostic
    if record["retry_parse"]:
        failure = "page_analysis_failed"
    elif record["errors"]:
        code = record["errors"][0]["reason"]
        failure = code if re.fullmatch(r"(config|oauth|completion):[a-z_]{1,64}", code) else "model_failed"
    elif pending:
        failure = "request_budget_exhausted"
    else:
        failure = None

    status = "failed" if failure else "needs_review" if ocr or needs_review or issues else "completed"
    queue.update(
        job, status=status, step="finished", diagnostic=failure or diagnostic, retry_allowed=bool(failure),
        coverage=record["extraction_coverage"], counts=counts, pages_needing_ocr=ocr, pages=pages,
        human_review_required=bool(results or issues or ocr or pending),
    )


class UploadWorker:
    def __init__(self, uploads: UploadStore, *, model_enabled=False, max_requests=8):
        if uploads.processing is None:
            raise ValueError("SPI_UPLOAD_PROCESS_STATE_DIR is required")
        if not 1 <= max_requests <= 32:
            raise ValueError("SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS must be between 1 and 32")
        self.uploads = uploads
        self.queue = UploadQueue(uploads.processing)
        self.model_enabled = model_enabled
        self.max_requests = max_requests

    def run_one(self) -> bool:
        with self.queue.claim() as job:
            if job is None:
                return False
            try:
                self._process(job)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                code = str(exc) if isinstance(exc, ProcessingFailure) else "processing_error"
                logger.warning("Upload job %s failed: %s (%s)", job.processing.job_id, code, type(exc).__name__)
                self.queue.update(job, status="failed", step="finished", diagnostic=code, retry_allowed=True)
            return True

    def _process(self, job):
        upload, path, content = _input(self.uploads, job)
        settings = GigaChatSettings.from_env() if self.model_enabled else None
        model_available = bool(settings and settings.credentials.get_secret_value().strip())
        mode = job.processing.mode or ("gigachat" if model_available else "local")
        if mode == "gigachat" and not model_available:
            raise ProcessingFailure("model_unavailable_for_recovery")
        diagnostic = job.processing.diagnostic
        if self.model_enabled and not model_available:
            diagnostic = "model_credentials_missing_local"
        self.queue.update(job, step="extracting", mode=mode, diagnostic=diagnostic)
        store = UploadProcessingStore(self.queue.root, upload, create=True)
        with ExitStack() as resources:
            client = None

            def complete(prompt, *, schema):
                nonlocal client
                if client is None:
                    client = resources.enter_context(GigaChatClient(settings, max_completion_retries=0))
                # Save the reservation before any external completion, including on
                # crashes that occur before batch can save a response checkpoint.
                self.queue.update(job, reserve_model_request=True)
                return client.complete_json(prompt, schema=schema)

            run_batch(
                store, [path], upload.domain, local_only=mode == "local",
                complete=complete, max_requests=max(0, self.max_requests - job.model_requests),
                pdf_content=content, retry_invalid_completion=True,
            )
        self.queue.update(job, step="checking_results")
        _finish(self.queue, job, store)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one available job and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    load_project_env()
    try:
        if os.getenv("SPI_UPLOAD_ENABLED", "").strip().lower() not in {"1", "true"}:
            parser.error("SPI_UPLOAD_ENABLED must be true")
        upload_token()
        worker = UploadWorker(
            UploadStore.from_env(),
            model_enabled=os.getenv("SPI_UPLOAD_PROCESS_GIGACHAT", "").strip().lower() in {"1", "true"},
            max_requests=int(os.getenv("SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS", "8")),
        )
    except (OSError, RuntimeError, ValueError):
        parser.error("Invalid private upload configuration. Check token, directories and model request limit (1–32).")

    logger.info("Private upload worker ready")
    try:
        while True:
            worked = worker.run_one()
            if args.once:
                return
            if not worked:
                sleep(1)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
