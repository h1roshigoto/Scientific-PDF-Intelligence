"""Read API; no fabricated scientific extraction results."""
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from .storage import Store

app = FastAPI(title="Scientific PDF Intelligence API", version="0.1.0")


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
    return store().results(domain)


@app.get("/results/{result_id}")
def result_detail(result_id: str):
    for row in store().results():
        if row.result_id == result_id:
            return row
    raise HTTPException(404, "Result not found")


@app.get("/issues")
def issues(domain: str | None = Query(None, pattern="^(solar|rag)$")):
    return store().issues(domain)


@app.get("/documents/{document_id}/file")
def document_file(document_id: str):
    for item in store().documents():
        if item["document_id"] == document_id:
            path = Path(item["file_path"])
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise HTTPException(404, "Original PDF unavailable")
            return FileResponse(path, media_type="application/pdf", filename=item["filename"])
    raise HTTPException(404, "Document not found")


@app.get("/export/{artifact}")
def export(artifact: str):
    if artifact not in ("documents.json", "results.json", "issues.json"):
        raise HTTPException(404, "Official CSV export requires participant templates")
    return store().read(artifact)
