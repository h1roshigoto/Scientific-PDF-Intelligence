"""Read API; no fabricated scientific extraction results."""
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from .config import load_project_env
from .project_passport import (
    AmbiguousPublication, PublicationNotFound, passport_filename, render_project_passport,
)
from .storage import Store

load_project_env()
app = FastAPI(title="Scientific PDF Intelligence API", version="0.1.0")
logger = logging.getLogger(__name__)
origins = [origin.strip() for origin in os.getenv("SPI_CORS_ORIGINS", "").split(",") if origin.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"],
                       expose_headers=["X-SPI-Generated-At"])


def store():
    return Store(os.environ.get("SPI_STATE_DIR", "state"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/documents")
def documents(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return [d for d in store().documents() if domain is None or d["domain"] == domain]


@app.get("/results")
def results(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return [_result_payload(row) for row in store().results(domain, include_excluded=False)]


def _result_payload(row):
    return row.model_dump(exclude={"extraction_metadata"} if row.extraction_metadata is None else None)


@app.get("/results/{result_id}")
def result_detail(result_id: str):
    for row in store().results(include_excluded=False):
        if row.result_id == result_id:
            return _result_payload(row)
    raise HTTPException(404, "Result not found")


@app.get("/issues")
def issues(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return store().issues(domain, include_excluded=False)


@app.get("/documents/{document_id}/file")
def document_file(document_id: str):
    for item in store().documents():
        if item["document_id"] == document_id:
            path = Path(item["file_path"])
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise HTTPException(404, "Original PDF unavailable")
            return FileResponse(path, media_type="application/pdf", filename=item["filename"],
                                content_disposition_type="inline")
    raise HTTPException(404, "Document not found")


@app.get("/export/project-passport.pdf", response_class=Response,
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
        content = render_project_passport(store(), run_id=run_id, domain=domain,
                                          publication_id=publication_id, generated_at=generated_at,
                                          version=app.version)
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


@app.get("/export/{artifact}")
def export(artifact: str):
    if artifact not in ("documents.json", "results.json", "issues.json"):
        raise HTTPException(404, "Official CSV export requires participant templates")
    return store().read(artifact)
