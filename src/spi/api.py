"""Scientific read API and private PDF uploads."""
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from .auth_api import SecurityMiddleware, install_auth
from .auth_config import AuthConfig, auth_mode, flag
from .config import load_project_env
from .project_passport import (
    AmbiguousPublication, PublicationNotFound, passport_filename, render_project_passport,
)
from .storage import Store
from .upload_api import router as upload_router
from .upload_store import mupdf_lock

VERSION = "0.1.0"
official_router = APIRouter()
logger = logging.getLogger(__name__)


def store():
    return Store(os.environ.get("SPI_STATE_DIR", "state"))


def health():
    return {"status": "ok"}


@official_router.get("/documents")
def documents(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return [d for d in store().documents() if domain is None or d["domain"] == domain]


@official_router.get("/results")
def results(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return [_result_payload(row) for row in store().results(domain, include_excluded=False)]


def _result_payload(row):
    return row.model_dump(exclude={"extraction_metadata"} if row.extraction_metadata is None else None)


@official_router.get("/results/{result_id}")
def result_detail(result_id: str):
    for row in store().results(include_excluded=False):
        if row.result_id == result_id:
            return _result_payload(row)
    raise HTTPException(404, "Result not found")


@official_router.get("/issues")
def issues(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return store().issues(domain, include_excluded=False)


@official_router.get("/documents/{document_id}/file")
def document_file(document_id: str):
    for item in store().documents():
        if item["document_id"] == document_id:
            file_path = item.get("file_path")
            if not file_path:
                raise HTTPException(404, "Original PDF unavailable")
            path = Path(file_path)
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise HTTPException(404, "Original PDF unavailable")
            return FileResponse(path, media_type="application/pdf", filename=item["filename"],
                                content_disposition_type="inline")
    raise HTTPException(404, "Document not found")


@official_router.get("/export/project-passport.pdf", response_class=Response,
         responses={200: {"content": {"application/pdf": {}}}})
def project_passport(
    run_id: str = Query(min_length=1),
    domain: Literal["all", "solar", "rag"] = "all",
    publication_id: str | None = Query(None, min_length=1),
):
    active_run = os.getenv("SPI_RUN_ID", "").strip()
    if not active_run:
        raise HTTPException(503, "Project passport is disabled. Configure SPI_RUN_ID for SPI_STATE_DIR.")
    if run_id != active_run:
        raise HTTPException(404, "Run ID does not match the active Store")

    generated_at = datetime.now(timezone.utc)
    filename = passport_filename(run_id, domain, publication_id)
    try:
        with mupdf_lock:
            content = render_project_passport(store(), run_id=run_id, domain=domain,
                                              publication_id=publication_id, generated_at=generated_at,
                                              version=VERSION)
    except PublicationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except AmbiguousPublication as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to generate the project passport")
        raise HTTPException(500, "Could not generate a complete project passport") from exc
    disposition = f'attachment; filename="{filename}"' if filename.isascii() else (
        f"attachment; filename*=UTF-8''{quote(filename)}"
    )
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": disposition,
        "X-SPI-Generated-At": generated_at.isoformat(),
        "Cache-Control": "no-store",
    })


@official_router.get("/export/{artifact}")
def export(artifact: str):
    if artifact not in ("documents.json", "results.json", "issues.json"):
        raise HTTPException(404, "Official CSV export requires participant templates")
    return store().read(artifact)


def create_app():
    multiuser = auth_mode() == "multiuser"
    app = FastAPI(title="Scientific PDF Intelligence API", version=VERSION,
                  openapi_url=None if multiuser else "/openapi.json",
                  redirect_slashes=not multiuser)
    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(upload_router)
    if multiuser:
        config = AuthConfig.from_env()
        install_auth(app, config)
        app.add_middleware(
            CORSMiddleware, allow_origins=list(config.origins), allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-SPI-CSRF", "X-SPI-Snapshot-SHA256", "X-SPI-Session-ID"],
            expose_headers=["X-SPI-Upload-Max-Bytes", "X-SPI-Snapshot-SHA256", "Content-Disposition", "Retry-After"],
        )
        app.add_middleware(SecurityMiddleware, config=config)
        if flag("SPI_DEPLOYMENT"):
            from .ops.runtime import auth_ready
            from .ops.telemetry import RequestMetrics

            # Deployment owns a fixed-size tmpfs. Keep tempfile from trying
            # fallback mount points when it fills during multipart spooling.
            tempfile.tempdir = "/tmp"

            @app.get("/__ops/ready", include_in_schema=False)
            def readiness(request: Request):
                if request.client is None or request.client.host != "127.0.0.1":
                    raise HTTPException(404, "not_found")
                try:
                    auth_ready(config)
                except (OSError, sqlite3.Error, ValueError):
                    return JSONResponse({"status": "unavailable"}, status_code=503)
                return {"status": "ready"}

            app.add_middleware(RequestMetrics)
    else:
        app.include_router(official_router)
        app.add_api_route("/auth/config", lambda: JSONResponse(
            {"mode": "private"}, headers={"Cache-Control": "no-store"}), methods=["GET"])
        origins = [origin.strip() for origin in os.getenv("SPI_CORS_ORIGINS", "").split(",") if origin.strip()]
        if origins:
            app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"],
                               expose_headers=["X-SPI-Generated-At"])
    return app


load_project_env()
app = create_app()
