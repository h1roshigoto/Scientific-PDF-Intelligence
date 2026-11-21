"""Authenticated, size-bounded access to the private upload store."""

import logging
import os
import re
import sqlite3
from secrets import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .models import UPLOAD_ID_PATTERN, StoredUpload
from .upload_store import InvalidPdf, UploadQuotaFull, UploadStore, UploadTooLarge

router = APIRouter(prefix="/uploads", tags=["private uploads"], responses={
    401: {"description": "Missing or invalid bearer token"},
    500: {"description": "Upload storage error"},
    503: {"description": "Private uploads disabled or misconfigured"},
})
logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
NO_STORE = {"Cache-Control": "no-store"}
STORAGE_ERRORS = (OSError, sqlite3.Error, ValidationError)
MULTIPART_OVERHEAD = 64 * 1024
UploadId = Annotated[str, Path(pattern=UPLOAD_ID_PATTERN)]


def _error(status: int, detail: str) -> HTTPException:
    headers = {**NO_STORE, "WWW-Authenticate": "Bearer"} if status == 401 else NO_STORE
    return HTTPException(status, detail, headers=headers)


def _authorized_store(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UploadStore:
    if os.getenv("SPI_UPLOAD_ENABLED", "").strip().lower() not in {"1", "true"}:
        raise _error(503, "uploads_disabled")
    token = os.getenv("SPI_UPLOAD_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
        raise _error(503, "uploads_misconfigured")
    if credentials is None or not compare_digest(credentials.credentials.encode(), token.encode()):
        raise _error(401, "upload_unauthorized")
    try:
        return UploadStore.from_env()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Invalid private upload configuration")
        raise _error(503, "uploads_misconfigured") from exc


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
        raise _error(500, "upload_storage_error") from exc

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
