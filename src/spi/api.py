"""Read API; no fabricated scientific extraction results."""
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from .config import load_project_env
from .storage import Store

load_project_env()
app = FastAPI(title="Scientific PDF Intelligence API", version="0.1.0")
origins = [origin.strip() for origin in os.getenv("SPI_CORS_ORIGINS", "").split(",") if origin.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"])


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


@app.get("/export/{artifact}")
def export(artifact: str):
    if artifact not in ("documents.json", "results.json", "issues.json"):
        raise HTTPException(404, "Official CSV export requires participant templates")
    return store().read(artifact)
