"""Extract PDF text blocks with coordinates; mark image-only pages for OCR."""
from hashlib import sha256
from pathlib import Path
import pymupdf
from .storage import Store


def parse_pdf(path, domain: str, *, content: bytes | None = None, inspect_page=None):
    if domain not in ("solar", "rag"):
        raise ValueError("Domain must be solar or rag")
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Expected an existing PDF")
    if content is None:
        content = path.read_bytes()
    document_id = f"{domain}:{path.stem}"
    pages = []
    with pymupdf.open(stream=content, filetype="pdf") as document:
        if document.needs_pass or not document.is_pdf or not len(document):
            raise ValueError("PDF is encrypted, empty or not readable")
        object_count = document.xref_length()
        for index in range(len(document)):
            item = {"page": index + 1, "blocks": [], "ocr_status": "unknown"}
            try:
                page = document[index]
                # MuPDF can load a blank placeholder for a broken page-tree entry.
                if not 0 < page.xref < object_count or document.xref_get_key(page.xref, "Type") != ("name", "/Page"):
                    raise ValueError("Invalid PDF page reference")
                blocks = [
                    {"text": block[4].strip(), "bbox": [round(float(x), 2) for x in block[:4]]}
                    for block in page.get_text("blocks", sort=True)
                    if block[6] == 0 and isinstance(block[4], str) and block[4].strip()
                    and any(ch.isalnum() and ch != "\ufffd" for ch in block[4])
                ]
                has_image = any(info["width"] >= 64 and info["height"] >= 64
                                for info in page.get_image_info()) if not blocks else False
                has_vectors = bool(page.get_drawings()) if not blocks and not has_image else False
                status = "text" if blocks else "ocr_required" if has_image or has_vectors else "empty"
                item.update(blocks=blocks, read_status=status,
                            ocr_status="pending" if status == "ocr_required" else "not_needed")
            except (RuntimeError, ValueError, IndexError) as exc:
                item.update(read_status="error", error=f"{type(exc).__name__}: {exc}")
            if inspect_page and item.get("read_status") == "text":
                try:
                    inspect_page(page, item["blocks"])
                except (RuntimeError, ValueError, IndexError, TypeError) as exc:
                    item["analysis_error"] = f"{type(exc).__name__}: {exc}"
            pages.append(item)
    record = {
        "document_id": document_id, "publication_id": path.stem,
        "domain": domain, "filename": path.name, "file_path": str(path),
        "sha256": sha256(content).hexdigest(),
        "parser_version": "pymupdf-2",
        "page_count": len(pages),
        "pages_needing_ocr": [p["page"] for p in pages if p["ocr_status"] == "pending"],
        "processing_status": "partial" if any(p["read_status"] in ("error", "ocr_required")
                                               or p.get("analysis_error") for p in pages) else "parsed_text",
    }
    return record, pages


def import_pdf(store: Store, path, domain: str, *, content: bytes | None = None):
    record, pages = parse_pdf(path, domain, content=content)
    # Legacy import clients count every page without a text layer as pending OCR.
    for page in pages:
        if page["read_status"] == "empty":
            page["ocr_status"] = "pending"
    record["pages_needing_ocr"] = [page["page"] for page in pages if page["ocr_status"] == "pending"]
    store.save_document(record, pages)
    return record
