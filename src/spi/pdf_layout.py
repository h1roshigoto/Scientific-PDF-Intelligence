"""Extract layout locally or through the deployment PDF boundary."""

from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from . import pdf_boundary
from .batch_evidence import relevant
from .batch_pdf import Fragment, page_fragments
from .pdf import parse_pdf

Box = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]


class _LayoutModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class _Block(_LayoutModel):
    text: str
    bbox: Box


class _Page(_LayoutModel):
    page: Annotated[int, Field(strict=True, ge=1, le=pdf_boundary.MAX_PAGES)]
    blocks: list[_Block]
    read_status: Literal["text", "empty", "ocr_required", "error"]
    ocr_status: Literal["unknown", "pending", "not_needed"]
    error: str | None = None
    analysis_error: str | None = None


class _Issue(_LayoutModel):
    reason: Literal["table_layout_unresolved"]
    page: Annotated[int, Field(strict=True, ge=1, le=pdf_boundary.MAX_PAGES)]
    location: str
    quote: str
    source_block_id: str


class _Layout(_LayoutModel):
    status: Literal["ok"]
    pages: Annotated[list[_Page], Field(min_length=1, max_length=pdf_boundary.MAX_PAGES)]
    fragments: list[Fragment]
    issues: list[_Issue]


def read_layout(path, domain, content=None):
    fragments, issues = [], []
    previous_captions = {}

    def inspect_page(page, blocks):
        units, unresolved = page_fragments(page, blocks, lambda text: relevant(text, domain),
                                           previous_captions=previous_captions, row_metrics=domain == "rag")
        fragments.extend(units)
        issues.extend(unresolved)

    metadata, pages = parse_pdf(path, domain, content=content, inspect_page=inspect_page)
    return metadata, pages, fragments, issues


def remote_layout(path, domain, content):
    result = pdf_boundary.request("layout", content, domain=domain)
    if result["status"] == "invalid":
        raise ValueError("pdf_parser_invalid_document")
    layout = _Layout.model_validate(result)
    count = len(layout.pages)
    if ([page.page for page in layout.pages] != list(range(1, count + 1))
            or any(not 1 <= fragment.page <= count for fragment in layout.fragments)
            or any(issue.page > count for issue in layout.issues)):
        raise ValueError("pdf_parser_invalid_layout")
    pages = [page.model_dump(mode="json", exclude_none=True) for page in layout.pages]
    issues = [issue.model_dump() for issue in layout.issues]
    metadata = {
        "document_id": f"{domain}:{path.stem}", "publication_id": path.stem,
        "domain": domain, "filename": path.name, "file_path": str(path),
        "sha256": sha256(content).hexdigest(), "parser_version": "pymupdf-2",
        "page_count": count, "pages_needing_ocr": [page.page for page in layout.pages if page.ocr_status == "pending"],
        "processing_status": "partial" if any(page.read_status in ("error", "ocr_required")
                                               or page.analysis_error for page in layout.pages) else "parsed_text",
    }
    return metadata, pages, layout.fragments, issues
