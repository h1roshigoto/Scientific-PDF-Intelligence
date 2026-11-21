"""Serve the production API with a fresh, synthetic Store for Playwright."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import uvicorn

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


if __name__ == "__main__":
    with TemporaryDirectory(prefix="spi-playwright-") as folder:
        store = seed(Path(folder))
        os.environ.update(SPI_STATE_DIR=str(store.root), SPI_RUN_ID="playwright-e2e")
        uvicorn.run("spi.api:app", host="127.0.0.1", port=8182)
