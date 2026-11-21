"""Validate model proposals against physical PDF evidence and track-specific context."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .batch_pdf import CAPTION, CELL, RECORD, RETRIEVER_AXES, UNIT_ONLY, Fragment
from .csv_schema import load_schema
from .gigachat_evidence import Block, EvidenceError, _NUMBER
from .models import Evidence, ExtractionMetadata, Metric, ResultRecord, Value


SCHEMA_VERSION = "batch-evidence-9"
MODEL_PLAN_VERSION = "table-candidate-v2"
PROFILES = {"solar": "solar-thin-film-v1", "rag": "rag-evaluation-v1"}
SOLAR = {
    "PCE": r"PCE|power conversion efficiency|efficiency",
    "Voc": r"V[ _]*oc|open.circuit voltage", "Jsc": r"(?:integrated )?J[ _]*sc(?: from (?:IPCE|EQE))?|short.circuit current density",
    "FF": r"FF|fill factor", "EQE": r"EQE|IPCE|external quantum efficiency",
    "bandgap": r"E[ _]*g|band ?gap", "series resistance": r"R[ _]*s|series resistance",
    "shunt resistance": r"R[ _]*sh|shunt resistance",
}
RAG = {
    "EM": r"EM|exact match(?: score)?", "F1": r"F[ -]?1(?: score)?",
    "accuracy": r"acc(?:uracy)?\.?", "precision": r"precision", "recall": r"recall",
    "MRR": r"MRR|mean reciprocal rank", "MAP": r"MAP|mean average precision",
    "nDCG": r"nDCG", "hit rate": r"hits?|hit rate", "ROUGE-L": r"ROUGE[- ]L",
    "BLEU": r"BLEU", "ECE": r"ECE|expected calibration error", "latency": r"latency(?: per query)?",
}
METRICS = {"solar": SOLAR, "rag": RAG}
_UNITLESS_RAG_SCORES = {"EM", "F1"}
UNIT_TAIL = re.compile(r"[\[(]([^()\[\]]+)[\])](?:\s*[↑↓])?\s*$")
SUPPLEMENT = re.compile(r"\b(?:supplement(?:ary|al) (?:material|information|data|table|figure)|supporting information)", re.I)
PROCEDURE = re.compile(
    r"\b(?:we (?:measure[ds]?|evaluat\w*|test\w*|obtain\w*|report\w*|conduct\w*|assess\w*|"
    r"compar\w*|present\w*|reimplement\w*|follow\w*)|our (?:experiments?|results?|measurements?|evaluation)|"
    r"measured under|parameters of .{0,155} under simulated|device parameters of the different .{0,70}\s+solar cells|"
    r"summary of photovoltaic performance for champion devices|main results of multi-hop QA datasets|"
    r"our training complements retrieval augmentation|exhibited PCE\s*=)", re.I | re.S,
)
CITED = re.compile(r"\b(?:taken|copied|sourced|quoted|obtained|reported) (?:directly )?(?:from|by)\b", re.I)
TABLE_REFERENCE = re.compile(r"\bTable\s+([A-Z]?\d+[a-z]?)\b", re.I)
LINKED_PROCEDURE = re.compile(
    r"\bwe (?:\w+ ){0,2}(?:use|compar\w*|evaluat\w*|measur\w*|report\w*|run|test\w*)\b"
    r"|\b(?:J\s*-\s*V|photocurrent density\s*-\s*photovoltage) curves"
    r"\b.{0,100}\b(?:are|were) (?:illustrated|shown|reported)\b", re.I,
)
RECALL_DEFINITION = re.compile(
    r"\brecall\s*@\s*k\s*(?:is|denotes)\s+(?:defined\s+as\s+)?(?:the\s+)?"
    r"(?:fraction|proportion)\s+of\s+(?:all\s+)?relevant\s+documents\b.{0,100}\btop\s+k(?:\b|(?=results\b))"
)
RECALL_SECTION_REFERENCE = re.compile(
    r"\brecall\s*@\s*k\s*\(?\s*(?:as\s+)?(?:defined|introduced)\s+in\s+section\s+"
    r"(\d+(?:\.\d+)*)(?=[\s,;.)]|$)(?!\.\d)"
)
SECTION_HEADING = re.compile(r"(?P<number>\d+(?:\.\d+)+)\s+[A-Z][^\n]{1,100}")
MODEL_NAME = r"(?:Llama|Qwen|GPT|Mistral|Gemini|T5|Claude|DeepSeek|Gemma|Phi)[\w.-]*"
GENERATOR_HEADER = re.compile(
    rf"{MODEL_NAME}(?:\s+(?:[vr]?\d[\w.-]*|plus|mini|nano|micro|small|medium|large|base|pro|flash|"
    r"turbo|instruct|chat|distill|preview|thinking|sonnet|haiku|opus))*", re.I,
)
RETRIEVER_HEADER = re.compile(r"\b(?:retrievers?|rerankers?|encoders?|embed(?:der|ding)?s?)\b", re.I)
NON_GENERATOR_COMPARISON = re.compile(
    r"\b(?:compar\w*|evaluat\w*|test\w*)\s+(?:(?:the|different|alternative|several|two)\s+)?"
    r"(?:(?P<retriever_model>retrieval (?:methods?|models?|backbones?|strategies?)|retrievers?)|"
    r"(?P<retrieval_method>(?:retrieval\s*(?:/\s*|and\s+))?rerank(?:ing|ers?))|"
    r"prompt(?:ing)? (?:methods?|strategies?|variants?))\b", re.I,
)
FIXED_GENERATOR = re.compile(
    r"\b(?:fixed|same|shared)\s+(?:generator|llm backbone)\b"
    r"|\b(?:generator|llm backbone)(?: model)?\s+(?:is\s+)?(?:held\s+|kept\s+)?fixed\b", re.I,
)
JV = re.compile(r"\b(?:j\s*-\s*v|photocurrent density\s*-\s*photovoltage)\b")


class Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    cell: str | None
    row: list[str] = Field(max_length=6)
    headers: list[str] = Field(max_length=6)
    block: str
    quote: str = Field(min_length=1, max_length=1250)
    raw_value: str = Field(min_length=1, max_length=45)
    raw_unit: str = Field(max_length=60)
    metric: str = Field(min_length=1, max_length=100)
    origin: Literal["primary", "cited", "unknown"]
    facts: dict[str, str] = Field(default_factory=dict, max_length=20)


class Reply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    results: list[Proposal] = Field(max_length=1)


class TableValueMismatch(EvidenceError):
    def __init__(self, proposal: Proposal, value: str, unit: str):
        self.fields = {
            field: {"expected": expected, "proposed": getattr(proposal, field)}
            for field, expected in (("raw_value", value), ("raw_unit", unit))
            if getattr(proposal, field) != expected
        }
        super().__init__("wrong_table_value_or_unit")


def _cell_blocks(cell):
    return (cell.value, *cell.row, *cell.headers, cell.row_axis)


def model_fragments(fragment: Fragment) -> list[Fragment]:
    if not fragment.cells:
        return [fragment]
    table_blocks = {block for cell in fragment.cells for block in _cell_blocks(cell)}
    context = (set(fragment.blocks) - table_blocks) | set(fragment.caption_support) | {fragment.caption}
    units = []
    for cell in fragment.cells:
        evidence = context.union(_cell_blocks(cell))
        units.append(replace(
            fragment, id=f"{fragment.id}:cell:{cell.value.block_id}", cells=(cell,),
            blocks=tuple(block for block in fragment.blocks if block in evidence),
        ))
    return units


def fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-").casefold().split())


def metric_label(text, domain):
    text = UNIT_TAIL.sub("", unicodedata.normalize("NFKC", text).strip()).strip()
    for metric, aliases in METRICS[domain].items():
        match = re.fullmatch(rf"(?:{aliases})(?:\s*@\s*(\d+))?", text, re.I)
        if match and (not match[1] or domain == "rag"):
            return metric, int(match[1]) if match[1] else None
    return None


def relevant(text, domain):
    metric_terms = "|".join(METRICS[domain].values())
    return bool(re.search(rf"(?<!\w)(?:{metric_terms})(?!\w)", text, re.I) and re.search(r"\d", text))


def _page_number(block):
    return int(block.block_id.split("-", 1)[0][1:])


def _block_ref(block):
    return {"block_id": block.block_id, "page": _page_number(block) if re.match(r"p\d+-", block.block_id) else None,
            "bbox": block.bbox, "text": block.text}


def _recall_scale_support(table, references, definitions, prose):
    section_refs = {
        section: block for block in references if set(TABLE_REFERENCE.findall(fold(block.text))) == {table}
        for section in RECALL_SECTION_REFERENCE.findall(fold(block.text))
    }
    candidates = []
    table_scope = re.compile(rf"(?:for|in) table\s+{re.escape(table)}\b,?\s+")
    for definition in definitions:
        text = fold(definition.text)
        tables = set(TABLE_REFERENCE.findall(text))
        sentences = re.split(r"(?<=[.!?])\s+(?=[a-z])", text)
        if tables == {table} and any(
            (scope := table_scope.match(sentence)) and RECALL_DEFINITION.match(sentence, scope.end())
            for sentence in sentences
        ):
            candidates.append((definition,))
        elif not tables and any(RECALL_DEFINITION.match(re.sub(r"^mathematically,\s*", "", sentence))
                                for sentence in sentences):
            # Only attach a lookup in a named section when the definition is directly under its heading.
            headings = [(match["number"], block) for block in prose[_page_number(definition)]
                        if 0 <= definition.bbox[1] - block.bbox[3] <= 24
                        and abs(definition.bbox[0] - block.bbox[0]) <= 8
                        and (match := SECTION_HEADING.fullmatch(block.text.strip()))]
            if len(headings) == 1 and headings[0][0] in section_refs:
                section, heading = headings[0]
                candidates.append((definition, heading, section_refs[section]))
    return candidates[0] if len(candidates) == 1 else ()


def link_table_context(fragments, pages, domain):
    prose = {}
    definitions = []
    for page in pages:
        number = page["page"]
        blocks = [Block(f"p{number}-text{i}-0", tuple(b["bbox"]), b["text"])
                  for i, b in enumerate(page["blocks"])]
        prose[number] = blocks
        definitions.extend(b for b in blocks if domain == "rag" and RECALL_DEFINITION.search(fold(b.text)))
    linked = {}
    recall_scales = {}
    for fragment in fragments:
        table = TABLE_REFERENCE.fullmatch(fragment.location or "")
        if not fragment.cells or not table:
            yield fragment
            continue
        key = (fragment.page, table[1].casefold())
        if key not in linked:
            references = []
            for page in range(fragment.page - 1, fragment.page + 2):
                for block in prose.get(page, ()):
                    if CAPTION.match(block.text) or key[1] not in TABLE_REFERENCE.findall(fold(block.text)):
                        continue
                    # A sentence can be split into adjacent text boxes in a single column.
                    neighbors = [b for b in prose[page] if 0 <= block.bbox[1] - b.bbox[3] <= 4
                                 and abs(b.bbox[0] - block.bbox[0]) < 4 and not re.search(r"[.!?]\s*$", b.text)
                                 and not CAPTION.match(b.text)] if re.match(r"[a-z]", block.text) else []
                    witness = (*neighbors, block) if len(neighbors) == 1 else (block,)
                    if _linked_procedure(witness, key[1]):
                        references.extend(witness)
            linked[key] = tuple(dict.fromkeys(references))
        references = linked[key]
        scale_support = ()
        if domain == "rag" and any(
            (label := _table_metric(cell, domain)) and label[0] == "recall" and label[1] is not None
            for cell in fragment.cells
        ):
            if key not in recall_scales:
                recall_scales[key] = _recall_scale_support(key[1], references, definitions, prose)
            scale_support = recall_scales[key]
        yield replace(fragment, blocks=tuple(dict.fromkeys((*fragment.blocks, *references, *scale_support))),
                      table_references=references, metric_scale_support=scale_support)


def _linked_procedure(blocks, table):
    text = fold(" ".join(b.text for b in blocks))
    if CITED.search(text):
        return False
    sentences = re.split(r"(?<=[.!?])\s+(?=[a-z])", text)
    if any(set(TABLE_REFERENCE.findall(sentence)) == {table} and LINKED_PROCEDURE.search(sentence)
           for sentence in sentences):
        return True
    if set(TABLE_REFERENCE.findall(text)) != {table} or not LINKED_PROCEDURE.search(text):
        return False
    # Across sentences, tie the publication's procedure to a table reporting those results.
    reference = rf"\btable\s+{re.escape(table)}\b"
    return bool(re.search(rf"\b(?:experimental|evaluation) results\s+in\s+{reference}"
                          rf"|{reference}\s+(?:lists|reports|shows)\s+(?:the|our)\b", text))


def _unit_key(unit):
    return fold(unit).replace(" ", "").replace("^", "").replace("·", "").replace("−", "-")


def normalize(raw_value, raw_unit, metric, domain):
    if not re.fullmatch(_NUMBER, raw_value):
        raise EvidenceError("unsupported_number")
    value = Decimal(raw_value.replace(",", ""))
    unit = _unit_key(raw_unit)
    if domain == "solar":
        scales = {
            "PCE": {"%": ("%", 1)}, "Voc": {"v": ("V", 1), "mv": ("V", ".001")},
            "Jsc": {u: ("mA/cm²", 1) for u in ("ma/cm2", "macm-2")},
            "FF": {"%": ("fraction", ".01"), "fraction": ("fraction", 1), "ratio": ("fraction", 1)},
            "EQE": {"%": ("%", 1)}, "bandgap": {"ev": ("eV", 1)},
            "series resistance": {"ωcm2": ("Ω cm²", 1), "ω": ("Ω", 1)},
            "shunt resistance": {"ωcm2": ("Ω cm²", 1), "ω": ("Ω", 1)},
        }
    else:
        scales = {"latency": {"ms": ("ms", 1), "s": ("ms", 1000)}}
        if metric in RAG and metric != "latency":
            if not unit and metric in _UNITLESS_RAG_SCORES and 0 <= value <= 1:
                # EM and F1 are unitless scores in this range. Canonicalize the decimal only.
                return Value(value=str(value))
            scales[metric] = {"%": ("%", 1), "fraction": ("fraction", 1), "ratio": ("fraction", 1)}
    if unit not in scales.get(metric, {}):
        raise EvidenceError("unknown_unit_or_scale")
    if (unit == "%" and not 0 <= value <= 100) or (unit in {"ratio", "fraction"} and not 0 <= value <= 1):
        raise EvidenceError("value_outside_scale")
    normalized_unit, factor = scales[metric][unit]
    return Value(value=str(value * Decimal(factor)), unit=normalized_unit)


def _table_unit(cell):
    header = UNIT_TAIL.search(cell.headers[-1].text) if cell.headers else None
    if cell.aggregation == "champion" and (record := RECORD.fullmatch(cell.value.text)):
        return record[1], header[1] if header else "", None
    number = CELL.fullmatch(cell.value.text)
    if number is None:
        raise EvidenceError("unsupported_number")
    raw_unit = number[3] or (header[1] if header else "")
    if number[3] and header and _unit_key(number[3]) != _unit_key(header[1]):
        raise EvidenceError("conflicting_units")
    return number[1], raw_unit, number[2]


def _table_metric(cell, domain):
    for b in reversed(cell.headers):
        if not UNIT_ONLY.fullmatch(b.text):
            return metric_label(b.text, domain)
    return None


def _origin(text):
    if CITED.search(text):
        return "cited"
    if PROCEDURE.search(text):
        return "primary"
    return "unknown"


def _table_origin(cell, fragment):
    caption = _support_caption(fragment)
    row = " ".join(b.text for b in cell.row)
    if _origin(row) == "cited":
        return "cited", cell.row
    if fragment.caption_has_reference or re.search(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]", caption + " " + row):
        return "unknown", ()
    proof = fragment.caption_support or ((fragment.caption,) if fragment.caption else ())
    origin = _origin(caption)
    if origin != "unknown":
        return origin, proof
    if fragment.table_references and not re.search(r"\([A-Z][^()]*,\s*(?:19|20)\d\d[a-z]?\)", row):
        return "primary", fragment.table_references
    return "unknown", ()


def _rag_header_context(cell, caption, axis, label):
    headers = [b.text for b in cell.headers if not UNIT_ONLY.fullmatch(b.text)]
    if len(headers) < 3 or not label or not re.search(r"\bdatasets?\b", caption, re.I):
        return {}
    if axis in RETRIEVER_AXES or axis in {"generator", "generator model", "llm backbone"}:
        return {}
    *model_parts, dataset, _ = headers
    if (not re.fullmatch(r"[\w][\w.-]*", dataset) or metric_label(dataset, "rag")
            or GENERATOR_HEADER.fullmatch(fold(dataset)) or RETRIEVER_HEADER.search(dataset)):
        return {}
    result = {"dataset_name": dataset}
    model = " ".join(" ".join(model_parts).split())
    # A model-shaped label alone cannot distinguish a generator from a retriever.
    answer_comparison = label[0] in _UNITLESS_RAG_SCORES and re.search(r"\bQA\b|question answering", caption, re.I)
    generator_comparison = re.search(r"\b(?:generators|llm backbones)\b", caption, re.I)
    comparison_caption = re.sub(r"(?:^|[;\n])\s*retriever(?: model)?\s*[:=][^;\n]+", "", caption, flags=re.I)
    role_conflict = (RETRIEVER_HEADER.search(comparison_caption)
                     or NON_GENERATOR_COMPARISON.search(comparison_caption)
                     or FIXED_GENERATOR.search(comparison_caption))
    if ((answer_comparison or generator_comparison) and not role_conflict
            and not RETRIEVER_HEADER.search(fold(model)) and GENERATOR_HEADER.fullmatch(fold(model))):
        result["generator_model"] = model
    return result


def _table_context(cell, fragment, domain):
    row = cell.row[-1].text
    headers = [b.text for b in cell.headers]
    caption = _support_caption(fragment)
    if domain == "solar":
        result = _labelled_context(caption, {
            "material_name_raw": "absorber(?: material)?", "formula_normalized": "composition",
            "architecture": "device architecture", "temperature": "temperature",
            "scan_rate": "scan rate", "stress_conditions": "stress conditions",
        })
        if result.get("architecture") not in {"n-i-p", "p-i-n", "substrate", "superstrate"}:
            result.pop("architecture", None)
        axis = fold(cell.row_axis.text) if cell.row_axis else ""
        if axis in {"direction", "scan direction", "sweep direction"}:
            result["raw_scan_direction"] = row
            if len(cell.row) == 2:
                result["device_label"] = cell.row[0].text
                if fold(row) in {"forward", "reverse", "average"}:
                    result["scan_direction"] = fold(row)
            champion = re.search(r"\bchampion devices?\b.{0,80}?\bbased on (.{1,120}?)\.?$", caption, re.I | re.S)
            if champion and len(cell.row) == 1:
                result.update(device_label=champion[1].strip(), device_role="record", aggregation="champion")
        else:
            result["device_label"] = row
        if cell.aggregation:
            result["aggregation"] = cell.aggregation
        direction = re.findall(r"\b(reverse|forward|average)\s+(?:J.?V\s+)?(?:scan|sweep)", " ".join([caption, *headers, row]), re.I)
        if len(set(map(str.lower, direction))) == 1 and "scan_direction" not in result:
            result["scan_direction"] = direction[0].lower()
        intensity = re.findall(rf"({_NUMBER})\s*mW\s*(?:/\s*cm[²2]|cm\s*[−⁻-]\s*[²2])", caption, re.I)
        if len(set(intensity)) == 1:
            result.update(light_intensity=intensity[0], intensity_unit="mW/cm²")
        spectrum = re.search(r"\bAM\s*1\.5\s*G\b", caption, re.I)
        if spectrum:
            result["spectrum"] = spectrum[0]
        label = _table_metric(cell, domain)
        spectral = any(re.search(r"\b(?:IPCE|EQE)\b", fold(h), re.I) for h in headers)
        if spectral:
            result["measurement_type"] = "EQE"
            for field in ("scan_direction", "raw_scan_direction", "light_intensity", "intensity_unit", "spectrum"):
                result.pop(field, None)
            if label and label[0] == "Jsc":
                result["note"] = "Current density integrated from the spectral response."
        elif (JV.search(fold(caption)) or any(JV.search(fold(b.text)) for b in fragment.table_references)
              or ("scan_direction" in result and label and label[0] in {"Voc", "Jsc", "FF", "PCE"}
                  and re.search(r"\b(?:pv|photovoltaic|solar cells?)\b", fold(caption)))):
            result["measurement_type"] = "J-V"
        if fragment.location and fragment.location.lower().startswith("fig"):
            stack = re.search(r"\bconsisting of an?\s+(.{5,150}?)\s+structure\b", caption, re.I | re.S)
            if stack and stack[1].count("/") >= 3:
                layers = []
                for i, text in enumerate(stack[1].split("/"), 1):
                    layer = {"layer_order": i, "layer_material": text.strip()}
                    process = re.search(r"\s*\((PED|RFMS)\)$", text.strip(), re.I)
                    if process:
                        layer.update(layer_material=text.strip()[:process.start()],
                                     deposition_method=process[1])
                    layers.append(layer)
                result.update(layers=layers, device_role="record", device_type="cell")
            result["note"] = "Figure-caption trace and corresponding prose: " + caption
        return result
    result = _labelled_context(caption, {
        "rag_variant": r"(?:rag )?(?:method|variant)",
        "retrieval_method": "retrieval method", "retriever_model": "retriever(?: model)?",
        "generator_model": "generator(?: model)?", "dataset_name": "dataset",
        "task_type": "task", "evaluation_protocol": "evaluation protocol",
        "metric_definition": "metric definition", "top_k": r"(?:retrieval )?top[ _-]k",
    })
    if "top_k" in result and not re.fullmatch(r"\d+", result["top_k"]):
        result.pop("top_k")
    caption_text = " ".join(unicodedata.normalize("NFKC", caption).split())
    datasets = re.findall(r"(?i:\bon\s+(?:(?:the )?(?:dataset|benchmark)\s+)?)([A-Z][\w-]*)"
                          r"(?=\s*(?:(?i:using|with|dataset|benchmark)\b|[.,;]|$))", caption_text)
    if len(set(datasets)) == 1:
        result.setdefault("dataset_name", datasets[0])
    axis = fold(cell.row_axis.text) if cell.row_axis else ""
    label = _table_metric(cell, domain)
    component = None
    if label and label[0] in {"recall", "precision", "MRR", "MAP", "nDCG", "hit rate"} and (
        axis in {"model type", "model", "method", "encoder", "retriever", "reranker"}
        and not result.get("generator_model")
    ):
        if re.search(r"\bperformance of (?:the )?rerank(?:er|ing)\b", fold(caption)):
            component = "reranking"
            result["retrieval_method"] = row
        elif re.search(r"\b(?:semantic search|text retrieval)\b.{0,65}\b(?:embedding|encoder)\b", fold(caption)):
            component = "retrieval"
            result["retriever_model"] = row
    if component:
        result["experiment_type"] = component
    else:
        if axis in {"generator", "generator model", "llm backbone"}:
            result.setdefault("generator_model", row)
        elif axis not in RETRIEVER_AXES and axis not in {"model", "model type", "dataset", "benchmark"}:
            result.setdefault("rag_variant", row)
        header_context = _rag_header_context(cell, caption, axis, label)
        pairs = re.findall(rf"(?i:\bon\s+){re.escape(result.get('dataset_name', ''))}"
                           rf"\s+(?i:using|with)\s+({MODEL_NAME})(?![\w.-])", caption_text)
        if result.get("dataset_name") and len(set(pairs)) == 1 and not header_context.get("generator_model"):
            result.setdefault("generator_model", pairs[0].rstrip("."))
        for field, value in header_context.items():
            if field in result and fold(result[field]) != fold(value):
                result.pop(field)
            else:
                result[field] = value
        if cell.model_header_unresolved:
            result.pop("generator_model", None)
    if re.search(r"multi-hop QA", caption, re.I):
        result["task_type"] = "multi-hop QA"
    return result


def _support_caption(fragment):
    return "\n".join(b.text for b in fragment.caption_support or
                     ((fragment.caption,) if fragment.caption else ()))


def _labelled_context(text, fields):
    context = {}
    for field, label in fields.items():
        matches = re.findall(rf"(?:^|[;\n])\s*(?:{label})\s*[:=]\s*([^;\n]+)", text, re.I)
        if len(set(matches)) == 1:
            context[field] = matches[0].strip()
    return context


def _prose_context(text, domain):
    if domain == "solar":
        subject = re.match(r"(?:For|On) (?:device|sample|cell) ([\w.-]+),\s+", text, re.I)
        if subject:
            return {"device_label": subject[1]}
    else:
        subject = re.match(
            r"For (?:method )?([\w.-]+) on ([\w.-]+) (?:with|using) (?:generator |model )?([\w.-]+),\s+",
            text, re.I,
        )
        if subject:
            return dict(zip(("rag_variant", "dataset_name", "generator_model"), subject.groups(), strict=True))
    return {}


def offline_proposals(fragment, domain):
    proposals = []
    for cell in fragment.cells:
        try:
            proposals.append(_table_proposal(cell, fragment, domain))
        except EvidenceError:
            continue
    return proposals


def _table_proposal(cell, fragment, domain):
    label = _table_metric(cell, domain)
    if label is None:
        leaf = next((b.text for b in reversed(cell.headers) if not UNIT_ONLY.fullmatch(b.text)), "")
        terms = re.split(r"\s*[/,]\s*|\s+and\s+", UNIT_TAIL.sub("", leaf), flags=re.I)
        ambiguous = not leaf or sum(metric_label(term, domain) is not None for term in terms) > 1
        code = "ambiguous_header" if ambiguous else "unknown_metric"
        raise EvidenceError(code)
    raw_value, raw_unit, _ = _table_unit(cell)
    metric, k = label
    return Proposal(
        cell=cell.value.block_id, row=[b.block_id for b in cell.row],
        headers=[b.block_id for b in cell.headers], block=cell.value.block_id, quote=cell.value.text,
        raw_value=raw_value, raw_unit=raw_unit, metric=f"{metric}@{k}" if k is not None else metric,
        origin=_table_origin(cell, fragment)[0], facts={},
    )


def unresolved_table_cells(fragment, domain, digest):
    for cell in fragment.cells:
        try:
            _table_proposal(cell, fragment, domain)
        except EvidenceError as exc:
            yield {"code": str(exc), "component": "metric" if str(exc) in {"ambiguous_header", "unknown_metric"}
                   else "value_or_unit", "pdf_sha256": digest, "page": fragment.page, "table": fragment.location,
                   "extraction_status": "needs_review",
                   "source_block_id": cell.value.block_id, "value": _block_ref(cell.value),
                   "row": [_block_ref(b) for b in cell.row], "headers": [_block_ref(b) for b in cell.headers]}


def make_prompt(fragment, domain):
    instructions = (
        "Return JSON {results: [...]} with at most one scientific measurement supported by the fragment. "
        "Each result has cell (null for prose), row and headers (arrays of block IDs in source order), block "
        "(the value block ID), quote (exact full table cell or complete prose claim), raw_value (the original "
        "numeric string), raw_unit (explicit unit or empty), metric, origin (primary, cited, or unknown), "
        "and optional facts (a dictionary of context field to exact source wording). "
        "Preserve the original spelling of raw_value, including leading decimal points and thousands separators. "
        "Do not normalize the number or infer missing units. Return an empty list if the claim cannot be grounded. "
    )
    if fragment.cells:
        candidates = offline_proposals(fragment, domain)
        instructions += (
            "Use only the listed target cell and its geometry. Copy raw_unit from the cell or header exactly, "
            "or use an empty string if neither prints a unit. Omit facts or use an empty object for a table cell. "
            "The row and column supply the measurement context. "
        )
        if len(candidates) == 1:
            instructions += (
                "Check this PDF-derived candidate against the source blocks. If supported, return it exactly.\n"
                + candidates[0].model_dump_json(exclude={"facts"})
            )
        return instructions + "\nPDF source:\n" + json.dumps(fragment.payload(), ensure_ascii=False)

    context = (
        "Solar keys include device_label, material_name_raw, formula_normalized, architecture, device_type, "
        "layer_order, layer_material, layer_function, deposition_method, annealing_temperature, "
        "measurement_type, scan_direction, spectrum, light_intensity, intensity_unit, aggregation, note."
        if domain == "solar" else
        "RAG keys include rag_variant, retrieval_method, retriever_model, generator_model, dataset_name, "
        "task_type, evaluation_protocol, metric_definition, top_k, note. Put metric K in metric as Name@K."
    )
    return (
        instructions
        + "Use empty row and header arrays for prose. "
        "For prose return one unambiguous assertion with one metric, its object and measurement conditions. "
        + context + "\nPDF source:\n" + json.dumps(fragment.payload(), ensure_ascii=False)
    )


def _facts(proposal, support, domain, context, errors):
    schema = load_schema(domain)
    tables = set(schema.tables) - {"publications", "issues"}
    allowed = {col for name in tables for col in schema.tables[name].columns
               if col not in {"record_ids", "duplicate_of"} and not col.endswith("_id")
               and not col.startswith(("raw_", "normalized_", "source_", "result_", "metric_"))}
    allowed.add("metric_definition")
    for key, phrase in proposal.facts.items():
        if key not in allowed or not phrase.strip() or not any(phrase in text for text in support):
            errors.append(f"unsupported_fact:{key}")
            continue
        # An exact string validates its occurrence, but cannot by itself establish a cross-field link.
        if key not in context:
            errors.append(f"unverified_fact_link:{key}")
        elif fold(str(context[key])) != fold(phrase):
            errors.append(f"conflicting_fact:{key}")


def verify(proposal, fragment, domain, document_id, digest, model_id):
    errors = []
    unconfirmed = {}
    context_evidence = {}
    label = metric_label(proposal.metric, domain)
    if label is None:
        raise EvidenceError("unknown_metric")
    name, k = label
    context = {}
    normalized = Value()
    source_block = next((b for b in fragment.blocks if b.block_id == proposal.block), None)
    if source_block is None or proposal.quote not in source_block.text:
        raise EvidenceError("quote_mismatch")
    origin_text = proposal.quote
    row = column = None
    geometry = [source_block]
    scale = proposal.raw_unit
    if fragment.cells:
        cell = next((cell for cell in fragment.cells if cell.value.block_id == proposal.cell), None)
        if cell is None or cell.value != source_block or proposal.quote != cell.value.text:
            raise EvidenceError("wrong_table_cell")
        if proposal.row != [b.block_id for b in cell.row] or proposal.headers != [b.block_id for b in cell.headers]:
            raise EvidenceError("wrong_table_cell")
        if _table_metric(cell, domain) != label:
            raise EvidenceError("wrong_table_metric")
        raw, unit, spread = _table_unit(cell)
        if (raw, unit) != (proposal.raw_value, proposal.raw_unit):
            raise TableValueMismatch(proposal, raw, unit)
        context.update(_table_context(cell, fragment, domain))
        geometry = [*cell.row, *cell.headers, source_block]
        if cell.row_axis:
            geometry.append(cell.row_axis)
        if fragment.caption:
            geometry.extend(fragment.caption_support or (fragment.caption,))
        geometry.extend(fragment.table_references)
        context_evidence["metric"] = [_block_ref(b) for b in cell.headers]
        if domain == "solar" and name == "PCE" and any(
            fold(UNIT_TAIL.sub("", b.text)) == "efficiency" for b in cell.headers
        ) and not (
            context.get("measurement_type") == "J-V"
            or re.search(r"\b(?:photovoltaic|power conversion|pv performance)\b", fold(_support_caption(fragment)))
        ):
            errors.append("solar_metric_context_unresolved")
            unconfirmed["metric"] = "The generic efficiency header has no confirmed link to power conversion."
        if spread:
            context["published_spread"] = spread
        if domain == "rag" and re.search(r"\bmean\s*(?:±|\+/-)\s*standard deviation\b", fold(_support_caption(fragment))):
            context["aggregation"] = "mean"
        if spread and "aggregation" not in context:
            errors.append("aggregation_unresolved")
            unconfirmed["aggregation"] = "The table does not identify the statistic accompanying the value."
        if (not unit and name == "recall" and k is not None and context.get("experiment_type")
                and fragment.metric_scale_support):
            scale = "fraction"
            context_evidence["metric_scale"] = [_block_ref(b) for b in fragment.metric_scale_support]
            geometry.extend(fragment.metric_scale_support)
        origin_text = _support_caption(fragment)
        origin, proof = _table_origin(cell, fragment)
        if proof:
            origin_text = "\n".join(b.text for b in proof)
            context_evidence["origin"] = [_block_ref(b) for b in proof]
        caption_blocks = fragment.caption_support or ((fragment.caption,) if fragment.caption else ())
        for field in ("dataset_name", "generator_model", "experiment_type"):
            if field in context:
                context_evidence[field] = [_block_ref(b) for b in (*caption_blocks, *cell.headers)]
        if "measurement_type" in context:
            jv_refs = [b for b in fragment.table_references if JV.search(fold(b.text))]
            context_evidence["measurement_type"] = [_block_ref(b) for b in (*caption_blocks, *cell.headers, *jv_refs)]
        if "scan_direction" in context:
            context_evidence["scan_direction"] = [_block_ref(b) for b in (*cell.row, *cell.headers)]
        row = " / ".join(b.text for b in cell.row)
        column = " / ".join(b.text for b in cell.headers)
    else:
        if proposal.cell is not None or proposal.row or proposal.headers:
            raise EvidenceError("unresolved_table")
        quote = proposal.quote
        if len(quote.split()) < 7:
            raise EvidenceError("incomplete_claim")
        found = re.findall(r"(?<![\w.,])" + re.escape(proposal.raw_value) + r"(?![\w.]|,\d)", quote)
        if len(found) != 1 or not relevant(quote, domain):
            raise EvidenceError("value_not_in_claim")
        aliases = METRICS[domain][name]
        unit_expr = re.escape(proposal.raw_unit) + r"(?![\w/%²])" if proposal.raw_unit else r"(?=[,.;)]|$)"
        # Require a metric-to-value assertion in the same clause, not numeric proximity.
        metric_expr = rf"(?<!\w)(?:{aliases})(?!\w)"
        assertion = re.search(
            rf"{metric_expr}\s*(?:@\s*\d+\s*)?(?:of\b|was\b|is\b|reached\b|=|:)\s*"
            rf"{re.escape(proposal.raw_value)}(?!\d|[.,]\d)\s*{unit_expr}", quote, re.I,
        )
        if assertion is None:
            errors.append("ambiguous_text_claim")
        if k is not None and not re.search(rf"{metric_expr}\s*@\s*{k}(?!\d)", quote, re.I):
            errors.append("metric_k_unverified")
        context.update(_prose_context(quote, domain))
        if not context:
            errors.append("prose_context_requires_review")
        origin = _origin(origin_text)
    if origin == "unknown" or origin != proposal.origin:
        errors.append("origin_unresolved")
        unconfirmed["origin"] = ("The table or claim does not establish whether the result is primary or cited."
                                 if origin == "unknown" else "The proposed origin conflicts with the source.")
    if fragment.cells and domain == "rag":
        required = {
            "retrieval": {"dataset_name", "retriever_model"},
            "reranking": {"dataset_name", "retrieval_method"},
        }.get(context.get("experiment_type"), {"rag_variant", "generator_model", "dataset_name"})
        missing = required - context.keys()
        if name in _UNITLESS_RAG_SCORES and "generator_model" in context and "experiment_type" not in context:
            # A caption can name fixed models without grounding the varying model for this cell.
            missing.update(match.lastgroup for match in NON_GENERATOR_COMPARISON.finditer(_support_caption(fragment))
                           if match.lastgroup)
        if missing:
            errors.append("rag_context_unresolved")
            unconfirmed.update(dict.fromkeys(sorted(missing), "The table does not establish this experimental role."))
    if fragment.cells and domain == "solar" and "device_label" not in context:
        errors.append("solar_device_unresolved")
        unconfirmed["device_label"] = "The row does not identify the device for this measurement."
    try:
        normalized = normalize(proposal.raw_value, scale, name, domain)
    except EvidenceError as exc:
        errors.append(str(exc))
        unconfirmed["metric_scale"] = (
            "No scale is printed for this cell or established by a definition linked to its table."
            if fragment.cells and name == "recall" and k is not None and not scale else str(exc)
        )
    _facts(proposal, [origin_text, *(b.text for b in geometry)], domain, context, errors)
    if proposal.facts:
        context["proposed_facts"] = proposal.facts
    context["evidence_blocks"] = [_block_ref(b) for b in dict.fromkeys(geometry)]
    if unconfirmed:
        context["unconfirmed_context"] = unconfirmed
    if context_evidence:
        context["context_evidence"] = context_evidence
    context["header_path"] = [b.text for b in cell.headers] if fragment.cells else []
    context["unit_status"] = "confirmed" if normalized.value is not None else "unconfirmed"
    context["origin_evidence"] = origin_text if origin != "unknown" else None
    figure = fragment.location if fragment.location and fragment.location.lower().startswith("fig") else None
    key = sha256(f"{fragment.id}|{proposal.block}|{proposal.quote}|{name}|{k}".encode()).hexdigest()[:20]
    result = ResultRecord(
        result_id=f"{document_id}:batch:{key}", document_id=document_id, domain=domain,
        metric=Metric(name=name, k=k), raw=Value(value=proposal.raw_value, unit=proposal.raw_unit or None),
        normalized=normalized if not errors else Value(),
        source=Evidence(page=fragment.page, table=fragment.location if fragment.cells and not figure else None,
                        figure=figure,
                        fragment=proposal.quote, row=row, column=column,
                        origin=origin if origin != "unknown" and origin == proposal.origin else None),
        context=context, extraction_status="needs_review" if errors else "verified",
        note=" | ".join(dict.fromkeys(errors)) if errors else None,
        extraction_metadata=ExtractionMetadata(
            model_id=model_id, method="pymupdf_gigachat_json_schema" if model_id != "local"
            else "pymupdf_verified_table", pdf_sha256=digest, schema_version=SCHEMA_VERSION,
            profile=PROFILES[domain], source_block_id=proposal.block,
        ),
    )
    return result, errors
