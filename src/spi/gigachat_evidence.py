"""Prepare and check the two D001 table lookups without trusting model coordinates."""

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pymupdf
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class Lookup:
    subject: str
    metric: str
    unit: str
    model: str = ""
    dataset: str = ""


SOLAR = Lookup("Pristine", "Voc", "mV")
RAG = Lookup("RA-CoT", "EM", "", model="LLaMA3-8B-Instruct", dataset="HotpotQA")


class Extraction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    raw_value: str = Field(min_length=1)
    raw_unit: str
    object_or_method: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    context: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)


@dataclass(frozen=True)
class Block:
    block_id: str
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True)
class Source:
    page: int
    blocks: tuple[Block, ...]
    target: Block


class EvidenceError(ValueError):
    """A static reason for manual review."""


_CAPTION = re.compile(r"^\s*Table\s+1\s*[.:|]", re.I)
_NUMBER = r"[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)"
_TOLERANCE = 2
_SOURCE_LIMIT = 6000
# Visually checked table bounds in PDF points for the two participant D001s.
# Labels and alignment must still match inside the crop.
_TABLE_REGIONS = {
    SOLAR: (44, 60, 291, 155),
    RAG: (71, 70, 524, 313),
}


def _label(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans("−–—‑", "----"))
    return " ".join(text.split())


def _cell_number(text: str, unit: str) -> str | None:
    suffix = rf"(?:\s*{re.escape(unit)})?" if unit else ""
    match = re.fullmatch(rf"({_NUMBER}){suffix}", text.strip())
    return match.group(1) if match else None


def _center_x(block: Block) -> float:
    return (block.bbox[0] + block.bbox[2]) / 2


def _center_y(block: Block) -> float:
    return (block.bbox[1] + block.bbox[3]) / 2


def _unique(blocks: Iterable[Block]) -> Block:
    matches = list(blocks)
    if len(matches) != 1:
        raise EvidenceError("table_cell_ambiguous" if matches else "table_cell_unresolved")
    return matches[0]


def _label_block(blocks: list[Block], label: str) -> Block:
    return _unique(block for block in blocks if _label(block.text) == _label(label))


def _row(blocks: list[Block], anchor: Block) -> list[Block]:
    return sorted(
        (block for block in blocks if abs(_center_y(block) - _center_y(anchor)) <= _TOLERANCE),
        key=_center_x,
    )


def _column_alignment(headers: list[Block], values: list[Block]) -> bool:
    return len(headers) == len(values) and all(
        abs(_center_x(header) - _center_x(value)) <= 4 for header, value in zip(headers, values)
    )


def _solar_column(blocks: list[Block], row: list[Block]) -> tuple[list[Block], int]:
    headers = _row(blocks, _label_block(blocks, "Sample"))
    metric = _unique(
        header for header in headers if re.fullmatch(r"v[\s_]*oc\s*\(\s*mv\s*\)", _label(header.text))
    )
    if _center_y(metric) >= _center_y(row[0]) or not _column_alignment(headers[1:], row[1:]):
        raise EvidenceError("table_column_unresolved")
    return headers, headers.index(metric)


def _aligned_groups(groups: list[Block], leaves: list[Block], size: int) -> bool:
    return len(leaves) == size * len(groups) and all(
        abs(_center_x(block) - (_center_x(leaves[i]) + _center_x(leaves[i + size - 1])) / 2) <= 5
        for block, i in zip(groups, range(0, len(leaves), size))
    )


def _rag_column(blocks: list[Block], row: list[Block], lookup: Lookup) -> tuple[list[Block], int]:
    model = _label_block(blocks, lookup.model)
    models = _row(blocks, model)
    dataset = next(
        (block for block in blocks if _label(block.text) == _label(lookup.dataset)), None,
    )
    metric = next((block for block in blocks if _label(block.text) == _label(lookup.metric)), None)
    if dataset is None or metric is None:
        raise EvidenceError("table_column_unresolved")
    datasets = _row(blocks, dataset)
    metrics = _row(blocks, metric)
    if (
        not _center_y(model) < _center_y(dataset) < _center_y(metric) < _center_y(row[0])
        or not _aligned_groups(models, datasets, 3)
        or not _aligned_groups(datasets, metrics, 2)
        or not _column_alignment(metrics, row[1:])
    ):
        raise EvidenceError("table_column_unresolved")
    first_dataset = models.index(model) * 3
    dataset_in_model = _label_block(datasets[first_dataset:first_dataset + 3], lookup.dataset)
    first_metric = datasets.index(dataset_in_model) * 2
    metric_in_dataset = _label_block(metrics[first_metric:first_metric + 2], lookup.metric)
    return [*models, *datasets, *metrics], metrics.index(metric_in_dataset) + 1


def _table_source(page: pymupdf.Page, lookup: Lookup) -> Source:
    prefix = f"p{page.number + 1}"
    region = pymupdf.Rect(_TABLE_REGIONS[lookup])
    blocks = []
    captions = []
    text = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for block in text["blocks"]:
        if not region.intersects(pymupdf.Rect(block["bbox"])):
            continue
        block_id = f"{prefix}-b{block['number']}"
        lines = []
        for index, line in enumerate(block.get("lines", ())):
            if pymupdf.Rect(line["bbox"]) in region:
                value = "".join(span["text"] for span in line["spans"]).strip()
                if value:
                    lines.append(Block(f"{block_id}-l{index}", tuple(round(v, 2) for v in line["bbox"]), value))
        if lines and _CAPTION.match(lines[0].text):
            captions.extend(lines)
        else:
            blocks.extend(lines)
    if sum(bool(_CAPTION.match(line.text)) for line in captions) != 1:
        raise EvidenceError("table_caption_unresolved")
    subject = _label_block(blocks, lookup.subject)
    row = _row(blocks, subject)
    if row[0] != subject or not all(_cell_number(block.text, lookup.unit) for block in row[1:]):
        raise EvidenceError("table_row_unresolved")
    headers, column = _rag_column(blocks, row, lookup) if lookup.model else _solar_column(blocks, row)
    if lookup.model and _center_y(captions[0]) <= _center_y(subject):
        raise EvidenceError("table_caption_unresolved")
    if not lookup.model and _center_y(captions[-1]) >= _center_y(headers[0]):
        raise EvidenceError("table_caption_unresolved")
    return Source(page.number + 1, (*captions, *headers, *row), row[column])


def prepare_source(path: Path, page_number: int, lookup: Lookup) -> Source:
    if not path.is_file():
        raise EvidenceError("pdf_missing")
    try:
        with pymupdf.open(path) as pdf:
            if not pdf.is_pdf or pdf.needs_pass:
                raise EvidenceError("pdf_unreadable")
            if not 1 <= page_number <= len(pdf):
                raise EvidenceError("page_missing")
            source = _table_source(pdf[page_number - 1], lookup)
            if len(json.dumps(source_payload(source), ensure_ascii=False)) > _SOURCE_LIMIT:
                raise EvidenceError("fragment_too_large")
            return source
    except (OSError, pymupdf.FileDataError) as exc:
        raise EvidenceError("pdf_unreadable") from exc


def source_payload(source: Source) -> dict:
    return {
        "pdf_page": source.page,
        "blocks": [
            {"block_id": block.block_id, "bbox": block.bbox, "text": block.text}
            for block in source.blocks
        ],
    }


def make_prompt(lookup: Lookup, source: Source) -> str:
    return (
        f"Extract {lookup.metric} for {lookup.subject} from the supplied table fragment."
        + (f" Use the dataset {lookup.dataset} and the model {lookup.model}." if lookup.model else "")
        + "\nFields: raw_value (exact numeric string, preserving commas and leading dots), "
        "raw_unit (unit as printed in cell or "
        "header, empty string when absent), object_or_method, metric, context (table and any "
        "model and dataset labels), source_block_id (value cell ID), source_quote (exact text "
        "from that cell, including the complete number and any unit printed in the cell)."
        "\nUse the block bounding boxes to match rows and columns. If the evidence is "
        "ambiguous, return {}.\nSource:\n"
        + json.dumps(source_payload(source), ensure_ascii=False)
    )


def quote_matches(result: Extraction, source: Source) -> bool:
    return bool(result.source_quote.strip()) and any(
        block.block_id == result.source_block_id and result.source_quote in block.text
        for block in source.blocks
    )


def normalize_cell(source: Source, lookup: Lookup) -> tuple[str, Decimal, str]:
    raw_value = _cell_number(source.target.text, lookup.unit)
    if raw_value is None:
        raise EvidenceError("value_or_unit_mismatch")
    value = Decimal(raw_value.replace(",", ""))
    if lookup.unit == "mV":
        return raw_value, value / Decimal(1000), "V"
    return raw_value, value, ""


def verify_extraction(result: Extraction, source: Source, lookup: Lookup) -> tuple[Decimal, str]:
    if not quote_matches(result, source):
        raise EvidenceError("quote_mismatch")
    if result.source_block_id != source.target.block_id:
        raise EvidenceError("wrong_table_cell")
    if result.source_quote.strip() != source.target.text.strip():
        raise EvidenceError("incomplete_cell_quote")
    context = _label(result.context)
    context_labels = (lookup.model, lookup.dataset)
    if _label(result.object_or_method) != _label(lookup.subject) or _label(result.metric) != _label(lookup.metric):
        raise EvidenceError("wrong_lookup")
    if not re.search(r"\btable\s+1\b", context) or not all(_label(label) in context for label in context_labels):
        raise EvidenceError("wrong_context")
    raw_value, value, unit = normalize_cell(source, lookup)
    if result.raw_value != raw_value or result.raw_unit != lookup.unit:
        raise EvidenceError("value_or_unit_mismatch")
    return value, unit
