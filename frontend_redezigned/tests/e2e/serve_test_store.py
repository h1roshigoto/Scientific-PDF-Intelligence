"""Serve the production API with a fresh, synthetic Store for Playwright."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import uvicorn
from fastapi.responses import FileResponse

from spi.models import ResultRecord, ValidationIssue
from spi.storage import Store


def seed(root: Path) -> Store:
    store = Store(root / "state")
    documents = []
    for domain, publication in (("solar", "D001"), ("rag", "D001"),
                                ("solar", "MISSING"), ("rag", "CORRUPT")):
        filename = f"{domain}-{publication}.pdf"
        path = root / filename
        if publication == "D001":
            with pymupdf.open() as pdf:
                for page in range(1, 4):
                    pdf.new_page().insert_text((72, 72), f"Test only {domain.upper()} {publication} page {page}")
                pdf.save(path)
        if publication == "CORRUPT":
            path.write_bytes(b"%PDF-1.7\nThis is a damaged test file.\n")
        documents.append({
            "document_id": f"{domain}:{publication}", "domain": domain, "publication_id": publication,
            "filename": filename, "title": f"Test only {domain.upper()} {publication}",
            "file_path": str(path), "page_count": 3,
        })
    store.write("documents.json", documents)

    rows = []
    for domain, result_id, metric, page, status in (
        ("solar", "e2e-solar-zero", "Voc", 2, "verified"),
        ("rag", "e2e-rag-review", "EM", 3, "needs_review"),
        ("rag", "e2e-rag-extracted", "F1", 2, "extracted"),
    ):
        solar = domain == "solar"
        result = ResultRecord(
            domain=domain, document_id=f"{domain}:D001", result_id=result_id,
            metric={"name": metric}, raw={"value": 0 if solar else "18.7", "unit": "mV" if solar else "%"},
            normalized={"value": 0, "unit": "V"} if solar else {}, extraction_status=status,
            context={"device_label": "E2E cell"} if solar else {"dataset_name": "E2E retrieval set"},
            source={"page": page, "fragment": f"Test only evidence for {result_id}",
                    "origin": "primary" if solar else "cited", "cited_publication_id": None if solar else "E2E-CITED"},
        )
        rows.append(result.model_dump(mode="json"))
    store.write("results.json", rows)
    issue = ValidationIssue(
        issue_id="e2e-issue", domain="rag", rule_id="e2e_evidence_check", reason="Test only review evidence",
        result_ids=["e2e-rag-review"], evidence_refs=[{"document_id": "rag:D001", "page": 3}],
    )
    store.write("issues.json", [issue.model_dump(mode="json")])
    return store


def processing_fixture(path: Path) -> None:
    with pymupdf.open() as pdf:
        first = pdf.new_page()
        first.insert_text((45, 70), "Synthetic processing fixture")
        first.insert_text((45, 90), "Erratum: B1 PCE was corrected from 15.6% to 15.7%.")
        page = pdf.new_page()
        page.insert_text((45, 70), "Table 1. Our measurements under simulated AM 1.5G of 100 mW/cm2")
        for y, cells in [(90, ("Sample", "Voc (mV)", "PCE (%)")),
                         (107, ("A1", "1200", "15.5")), (124, ("B1", "1200", "15.6"))]:
            for x, value in zip((45, 135, 215), cells):
                offset = pymupdf.get_text_length(value) / 2 if x > 50 else 0
                page.insert_text((x - offset, y), value)
        pdf.save(path)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="spi-playwright-") as folder:
        root = Path(folder)
        store = seed(root)
        os.environ.update(
            SPI_AUTH_MODE="private",
            SPI_STATE_DIR=str(store.root), SPI_RUN_ID="playwright-e2e",
            SPI_UPLOAD_ENABLED="true", SPI_UPLOAD_TOKEN="synthetic-playwright-token-0123456789",
            SPI_UPLOAD_DIR=str(root / "uploads"), SPI_UPLOAD_STATE_DIR=str(root / "upload-state"),
            SPI_UPLOAD_PROCESS_STATE_DIR=str(root / "processing"), SPI_UPLOAD_PROCESS_GIGACHAT="false",
            SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS="8",
            SPI_UPLOAD_MAX_BYTES="16384", SPI_UPLOAD_MAX_FILES="20",
        )
        from spi.api import app

        fixture = root / "processing-fixture.pdf"
        processing_fixture(fixture)

        @app.get("/__test/processing-fixture.pdf", include_in_schema=False)
        def fixture_pdf():
            return FileResponse(fixture, media_type="application/pdf")

        with subprocess.Popen([sys.executable, "-m", "spi.upload_worker"]) as worker:
            try:
                uvicorn.run(app, host="127.0.0.1", port=8182)
            finally:
                worker.terminate()
