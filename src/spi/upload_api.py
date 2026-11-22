"""Authenticated, size-bounded access to the private upload store."""

import logging
import os
import sqlite3
from contextlib import ExitStack, contextmanager, nullcontext
from secrets import compare_digest
from tempfile import TemporaryFile
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .auth_api import current_session, rate
from .auth_store import AuthError
from .models import UPLOAD_ID_PATTERN, ResultRecord, StoredUpload, UploadJobAccepted, UploadProcessing, ValidationIssue
from .review_models import ReviewDetail, ReviewedResult, ReviewEvent, ReviewOverview, ReviewRequest
from .upload_export import archive_filename, build_archive, build_artifact, capture_snapshot
from .upload_export_models import ManifestPreview
from .upload_processing import UploadProcessingStore
from .upload_queue import UploadQueue
from .upload_reviews import ReviewError, UploadReviews
from .tenant_storage import DAY, tenant_lease, tenant_queue, tenant_store
from .upload_store import InvalidPdf, UploadQuotaFull, UploadStore, UploadTooLarge, storage_lease, upload_token

router = APIRouter(prefix="/uploads", tags=["private uploads"], responses={
    401: {"description": "Missing or invalid bearer token"},
    500: {"description": "Upload storage error"},
    503: {"description": "Private uploads disabled or misconfigured"},
})
logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
NO_STORE = {"Cache-Control": "no-store"}
STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError)
MULTIPART_OVERHEAD = 64 * 1024


def _storage_error(exc, fallback):
    from .ops.state import disk_full
    from .pdf_boundary import ParserUnavailable

    if disk_full(exc):
        return _error(507, "storage_full")
    if isinstance(exc, ParserUnavailable):
        return _error(503, "pdf_parser_unavailable")
    return _error(500, fallback)
UploadId = Annotated[str, Path(pattern=UPLOAD_ID_PATTERN)]


def _error(status: int, detail: str) -> HTTPException:
    headers = {**NO_STORE, "WWW-Authenticate": "Bearer"} if status == 401 else NO_STORE
    return HTTPException(status, detail, headers=headers)


def _authorized_store(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    db = getattr(request.app.state, "auth", None)
    if db:
        if request.headers.get("authorization") is not None:
            raise AuthError(401, "session_required")
        owner = current_session(request, db).user.user_id
        path = request.url.path.rstrip("/")
        if "/export/" in path:
            category = "export"
        elif request.method == "POST":
            category = "upload" if path == "/uploads" else "process" if path.endswith("/process") else "review"
        else:
            category = "upload_read"
        rate(db, request, category, owner)
        quota = db.quotas(owner)
        request.state.upload_quota = quota
        with tenant_lease(db.config, owner):
            yield tenant_store(db.config, owner, quota)
        return
    if os.getenv("SPI_UPLOAD_ENABLED", "").strip().lower() not in {"1", "true"}:
        raise _error(503, "uploads_disabled")
    try:
        token = upload_token()
    except ValueError as exc:
        raise _error(503, "uploads_misconfigured") from exc
    if credentials is None or not compare_digest(credentials.credentials.encode(), token.encode()):
        raise _error(401, "upload_unauthorized")
    try:
        uploads = UploadStore.from_env()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Invalid private upload configuration")
        raise _error(503, "uploads_misconfigured") from exc
    with storage_lease(uploads.state) if uploads.state.exists() else nullcontext():
        yield uploads


PrivateStore = Annotated[UploadStore, Depends(_authorized_store)]


class _MultipartTooLarge(MultiPartException):
    pass


async def _bounded_body(request: Request, limit: int):
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _MultipartTooLarge("Multipart body too large")
        yield chunk


class _UploadMultipart(MultiPartParser):
    def __init__(self, request: Request, max_bytes: int):
        super().__init__(request.headers, _bounded_body(request, max_bytes + MULTIPART_OVERHEAD),
                         max_files=1, max_fields=1)
        self.max_part_size = 32
        self.max_bytes = max_bytes
        self.part_size = 0
        self.finished = False

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.part_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.part_size += end - start
        if self._current_part.file is not None and self.part_size > self.max_bytes:
            raise _MultipartTooLarge("PDF too large")
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.finished = True

    async def parse(self):
        try:
            form = await super().parse()
            if not self.finished:
                raise MultiPartException("Missing closing multipart boundary")
            return form
        except BaseException:
            # Older Starlette releases only clean up on MultiPartException.
            for spool in self._files_to_close_on_error:
                spool.close()
            raise


@router.post("", status_code=201, response_model=StoredUpload, responses={
    400: {"description": "Invalid multipart body"},
    409: {"description": "Upload count limit reached"},
    413: {"description": "Upload size limit exceeded"},
    415: {"description": "Expected multipart/form-data"},
    422: {"description": "Invalid upload fields or PDF"},
}, openapi_extra={
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file", "domain"], "additionalProperties": False,
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "domain": {"type": "string", "enum": ["solar", "rag"]},
        },
    }}}},
})
async def create_upload(request: Request, response: Response, uploads: PrivateStore) -> StoredUpload:
    content_type, _ = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data":
        raise _error(415, "multipart_required")

    try:
        form = await _UploadMultipart(request, uploads.max_bytes).parse()
        try:
            file, domain = form.get("file"), form.get("domain")
            if not isinstance(file, UploadFile) or domain not in ("solar", "rag"):
                raise _error(422, "invalid_upload_fields")
            upload = await run_in_threadpool(uploads.save, file.file, file.filename or "", domain)
        finally:
            await form.close()
    except (_MultipartTooLarge, UploadTooLarge) as exc:
        raise _error(413, "upload_too_large") from exc
    except (MultiPartException, MultipartParseError, ClientDisconnect) as exc:
        raise _error(400, "invalid_multipart") from exc
    except InvalidPdf as exc:
        raise _error(422, str(exc)) from exc
    except UploadQuotaFull as exc:
        raise _error(409, "upload_quota_full") from exc
    except STORAGE_ERRORS as exc:
        logger.error("Private upload write failed (%s)", type(exc).__name__)
        raise _storage_error(exc, "upload_storage_error") from exc

    response.headers.update(NO_STORE)
    return upload


@router.get("", response_model=list[StoredUpload], responses={
    200: {"headers": {"X-SPI-Upload-Max-Bytes": {"schema": {"type": "integer", "minimum": 1}}}},
})
def list_uploads(response: Response, uploads: PrivateStore) -> list[StoredUpload]:
    try:
        records = uploads.list()
    except STORAGE_ERRORS as exc:
        raise _error(500, "upload_storage_error") from exc
    response.headers.update(NO_STORE)
    response.headers["X-SPI-Upload-Max-Bytes"] = str(uploads.max_bytes)
    return records


def _get_upload(upload_id: str, uploads: UploadStore) -> StoredUpload:
    try:
        record = uploads.get(upload_id)
    except STORAGE_ERRORS as exc:
        raise _error(500, "upload_storage_error") from exc
    if record is None:
        raise _error(404, "upload_not_found")
    return record


@router.get("/{upload_id}", response_model=StoredUpload, responses={404: {"description": "Upload not found"}})
def upload_detail(upload_id: UploadId, response: Response, uploads: PrivateStore) -> StoredUpload:
    response.headers.update(NO_STORE)
    return _get_upload(upload_id, uploads)


@router.get("/{upload_id}/file", response_class=FileResponse, responses={
    200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
    404: {"description": "Upload or original file not found"},
})
def upload_file(upload_id: UploadId, uploads: PrivateStore) -> FileResponse:
    try:
        path = uploads.file(_get_upload(upload_id, uploads))
    except OSError as exc:
        raise _error(500, "upload_storage_error") from exc
    if path is None:
        raise _error(404, "upload_not_found")
    return FileResponse(path, media_type="application/pdf", filename=f"{upload_id}.pdf",
                        headers={**NO_STORE, "X-Content-Type-Options": "nosniff"})


def _queue(uploads: UploadStore, request: Request | None = None) -> UploadQueue:
    if uploads.processing is None:
        raise _error(503, "upload_processing_unconfigured")
    if uploads.owner_user_id is not None and request is not None:
        return tenant_queue(request.app.state.auth.config, uploads, request.state.upload_quota)
    return UploadQueue(uploads.processing, uploads.owner_user_id)


@router.post("/{upload_id}/process", status_code=202, response_model=UploadJobAccepted)
def process_upload(upload_id: UploadId, request: Request, response: Response, uploads: PrivateStore) -> UploadJobAccepted:
    upload = _get_upload(upload_id, uploads)
    try:
        state = _queue(uploads, request).enqueue(upload).processing
    except UploadQuotaFull as exc:
        raise _error(409, "processing_quota_full") from exc
    except STORAGE_ERRORS as exc:
        raise _error(500, "upload_processing_storage_error") from exc
    response.headers.update(NO_STORE)
    return UploadJobAccepted(upload_id=upload_id, job_id=state.job_id, status=state.status)


@router.get("/{upload_id}/processing", response_model=UploadProcessing)
def upload_processing(upload_id: UploadId, response: Response, uploads: PrivateStore) -> UploadProcessing:
    _get_upload(upload_id, uploads)
    try:
        job = _queue(uploads).get(upload_id)
    except STORAGE_ERRORS as exc:
        raise _error(500, "upload_processing_storage_error") from exc
    response.headers.update(NO_STORE)
    return job.processing if job else UploadProcessing(upload_id=upload_id, status="not_started")


def _scientific_data(upload_id: str, uploads: UploadStore, *, issues: bool) -> list:
    upload = _get_upload(upload_id, uploads)
    queue = _queue(uploads)
    try:
        job = queue.get(upload_id)
        if job is None or not job.matches(upload) or job.processing.counts is None:
            return []
        store = UploadProcessingStore(queue.root, upload)
        record = store.checkpoint()
        if record is None:
            raise ValueError("Missing private checkpoint for a finished job")
        rows = store.upload_issues(record)[0] if issues else store.upload_results()
        current = queue.get(upload_id)
        # A concurrent retry can supersede a finished attempt while reading its Store.
        return rows if current and current.processing.job_id == job.processing.job_id else []
    except STORAGE_ERRORS as exc:
        raise _error(500, "upload_processing_storage_error") from exc


@router.get("/{upload_id}/results", response_model=list[ResultRecord])
def upload_results(upload_id: UploadId, response: Response, uploads: PrivateStore):
    response.headers.update(NO_STORE)
    return _scientific_data(upload_id, uploads, issues=False)


@router.get("/{upload_id}/issues", response_model=list[ValidationIssue])
def upload_issues(upload_id: UploadId, response: Response, uploads: PrivateStore):
    response.headers.update(NO_STORE)
    return _scientific_data(upload_id, uploads, issues=True)


class _PrivateRoute(APIRoute):
    invalid_request = "invalid_review_request"

    def get_route_handler(self):
        handle = super().get_route_handler()

        async def review_request(request):
            try:
                return await handle(request)
            except RequestValidationError as exc:
                raise _error(422, self.invalid_request) from exc
            except StarletteHTTPException as exc:
                exc.headers = {**(exc.headers or {}), **NO_STORE}
                raise

        return review_request


review_router = APIRouter(route_class=_PrivateRoute)
ResultId = Annotated[str, Path(min_length=1, max_length=256)]


@contextmanager
def _reviews(upload_id, response, uploads, *, write=False):
    upload = _get_upload(upload_id, uploads)
    queue = _queue(uploads)
    response.headers.update(NO_STORE)
    try:
        with queue.review_snapshot(upload_id, write=write) as job:
            yield UploadReviews(uploads, upload, queue.root, job, write=write)
    except ReviewError as exc:
        raise _error(exc.status, exc.code) from exc
    except (*STORAGE_ERRORS, RuntimeError) as exc:
        logger.error("Private review operation failed (%s)", type(exc).__name__)
        raise _storage_error(exc, "upload_review_storage_error") from exc


@review_router.get("/{upload_id}/reviews", response_model=ReviewOverview)
def review_overview(upload_id: UploadId, response: Response, uploads: PrivateStore):
    with _reviews(upload_id, response, uploads) as reviews:
        return reviews.overview()


@review_router.get("/{upload_id}/results/{result_id}/review", response_model=ReviewDetail)
def review_detail(upload_id: UploadId, result_id: ResultId, response: Response, uploads: PrivateStore):
    with _reviews(upload_id, response, uploads) as reviews:
        return reviews.detail(result_id)


@review_router.post("/{upload_id}/results/{result_id}/review", status_code=201, response_model=ReviewDetail)
def save_review(upload_id: UploadId, result_id: ResultId, request: ReviewRequest, response: Response,
                uploads: PrivateStore):
    with _reviews(upload_id, response, uploads, write=True) as reviews:
        return reviews.save(result_id, request)


@review_router.get("/{upload_id}/results/{result_id}/review/history", response_model=list[ReviewEvent])
def review_history(upload_id: UploadId, result_id: ResultId, response: Response, uploads: PrivateStore):
    with _reviews(upload_id, response, uploads) as reviews:
        return reviews.history(result_id)


@review_router.get("/{upload_id}/reviewed-results", response_model=list[ReviewedResult])
def reviewed_results(upload_id: UploadId, response: Response, uploads: PrivateStore):
    with _reviews(upload_id, response, uploads) as reviews:
        return reviews.reviewed_results()


router.include_router(review_router)


class _ExportRoute(_PrivateRoute):
    invalid_request = "invalid_export_request"


export_router = APIRouter(prefix="/{upload_id}/export", route_class=_ExportRoute, responses={
    409: {"description": "Analysis unavailable, active or changed, or source no longer matches"},
    413: {"description": "Snapshot or artifact exceeds the export limits"},
})
SnapshotHeader = Annotated[str | None, Header(alias="X-SPI-Snapshot-SHA256", pattern=r"^[a-f0-9]{64}$")]


@contextmanager
def _export_operation():
    try:
        yield
    except ReviewError as exc:
        raise _error(exc.status, exc.code) from exc
    except (*STORAGE_ERRORS, RuntimeError) as exc:
        logger.error("Private export operation failed (%s)", type(exc).__name__)
        raise _storage_error(exc, "upload_export_storage_error") from exc


def _snapshot(upload_id, uploads, expected=None, *, request=None):
    upload = _get_upload(upload_id, uploads)
    if request is not None and upload.owner_user_id is not None:
        db = request.app.state.auth
        db.consume([("quota_export", upload.owner_user_id, request.state.upload_quota.max_exports_per_period, DAY)],
                   code="export_quota_full")
    queue = _queue(uploads)
    return capture_snapshot(uploads, upload, queue, expected=expected)


def _attachment(snapshot, filename, *, disposition="attachment"):
    return {
        **NO_STORE, "X-Content-Type-Options": "nosniff",
        "X-SPI-Snapshot-SHA256": snapshot.preview.metadata.snapshot_sha256,
        "Content-Disposition": f'{disposition}; filename="{filename}"',
    }


@export_router.get("/manifest", response_model=ManifestPreview)
def export_preview(upload_id: UploadId, response: Response, uploads: PrivateStore):
    with _export_operation():
        snapshot = _snapshot(upload_id, uploads)
        response.headers.update(_attachment(snapshot, "manifest-preview.json", disposition="inline"))
        return snapshot.preview


def _download(upload_id, uploads, expected, name, request):
    with _export_operation():
        snapshot = _snapshot(upload_id, uploads, expected, request=request)
        artifact = build_artifact(snapshot, name)
        return Response(artifact.content, media_type=artifact.media_type,
                        headers=_attachment(snapshot, artifact.filename))


@export_router.get("/reviewed-results.csv", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
def export_csv(upload_id: UploadId, uploads: PrivateStore, request: Request, snapshot: SnapshotHeader = None):
    return _download(upload_id, uploads, snapshot, "reviewed_results.csv", request)


@export_router.get("/reviewed-results.json", response_class=Response,
                   responses={200: {"content": {"application/json": {}}}})
def export_json(upload_id: UploadId, uploads: PrivateStore, request: Request, snapshot: SnapshotHeader = None):
    return _download(upload_id, uploads, snapshot, "reviewed_results.json", request)


@export_router.get("/scientific-passport.pdf", response_class=Response,
                   responses={200: {"content": {"application/pdf": {}}}})
def export_private_passport(upload_id: UploadId, uploads: PrivateStore, request: Request, snapshot: SnapshotHeader = None):
    return _download(upload_id, uploads, snapshot, "scientific-passport.pdf", request)


class _ProjectResponse(StreamingResponse):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        headers = _attachment(snapshot, archive_filename(snapshot.preview.metadata))
        super().__init__(iter(()), media_type="application/zip", headers=headers)

    async def __call__(self, scope, receive, send):
        # Scope the unnamed temporary file to the ASGI response, including a
        # client disconnect. Generation errors occur before headers are sent.
        with ExitStack() as resources:
            with _export_operation():
                output = resources.enter_context(TemporaryFile(mode="w+b"))
                size = await run_in_threadpool(build_archive, self.snapshot, output)
            self.headers["Content-Length"] = str(size)
            self.body_iterator = iterate_in_threadpool(iter(lambda: output.read(64 * 1024), b""))
            await super().__call__(scope, receive, send)


@export_router.get("/project.zip", response_class=StreamingResponse,
                   responses={200: {"content": {"application/zip": {}}}})
def export_project(upload_id: UploadId, uploads: PrivateStore, request: Request, snapshot: SnapshotHeader = None):
    with _export_operation():
        return _ProjectResponse(_snapshot(upload_id, uploads, snapshot, request=request))


router.include_router(export_router)
