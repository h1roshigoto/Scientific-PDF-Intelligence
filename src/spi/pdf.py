"""Extract PDF text blocks with coordinates; mark image-only pages for OCR."""
from hashlib import sha256
from pathlib import Path
import fitz
from .storage import Store


def import_pdf(store: Store, path, domain: str):
    if domain not in ("solar", "rag"):
        raise ValueError("Domain must be solar or rag")
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Expected an existing PDF")
    document_id = f"{domain}:{path.stem}"
    pages = []
    with fitz.open(path) as document:
        for index, page in enumerate(document):
            blocks = [
                {"text": block[4].strip(), "bbox": [round(float(x), 2) for x in block[:4]]}
                for block in page.get_text("blocks", sort=True)
                if isinstance(block[4], str) and block[4].strip()
            ]
            pages.append({
                "page": index + 1, "blocks": blocks,
                "ocr_status": "not_needed" if blocks else "pending",
            })
    record = {
        "document_id": document_id, "publication_id": path.stem,
        "domain": domain, "filename": path.name, "file_path": str(path),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "parser_version": "pymupdf-1",
        "page_count": len(pages),
        "pages_needing_ocr": [p["page"] for p in pages if p["ocr_status"] == "pending"],
        "processing_status": "parsed_text",
    }
    store.upsert("documents.json", record, "document_id")
    store.write(f"{domain}_{path.stem}_pages.json", pages)
    return record
