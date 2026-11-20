"""Validate model proposals against physical PDF evidence and track-specific context."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .batch_pdf import CELL, RECORD, Fragment
from .csv_schema import load_schema
from .gigachat_evidence import EvidenceError, _NUMBER
from .models import Evidence, ExtractionMetadata, Metric, ResultRecord, Value


SCHEMA_VERSION = "batch-evidence-3"
PROFILES = {"solar": "solar-thin-film-v1", "rag": "rag-evaluation-v1"}
SOLAR = {
    "PCE": r"PCE|power conversion efficiency|efficiency",
    "Voc": r"V[ _]*oc|open.circuit voltage", "Jsc": r"J[ _]*sc|short.circuit current density",
    "FF": r"FF|fill factor", "EQE": r"EQE|external quantum efficiency",
    "bandgap": r"E[ _]*g|band ?gap", "series resistance": r"R[ _]*s|series resistance",
    "shunt resistance": r"R[ _]*sh|shunt resistance",
}
RAG = {
    "EM": r"EM|exact match(?: score)?", "F1": r"F[ -]?1(?: score)?",
    "accuracy": r"acc(?:uracy)?\.?", "precision": r"precision", "recall": r"recall",
    "MRR": r"MRR|mean reciprocal rank", "MAP": r"MAP|mean average precision",
    "nDCG": r"nDCG", "hit rate": r"hits?|hit rate", "ROUGE-L": r"ROUGE[- ]L",
    "BLEU": r"BLEU", "latency": r"latency(?: per query)?",
}
METRICS = {"solar": SOLAR, "rag": RAG}
UNIT_TAIL = re.compile(r"[\[(]([^()\[\]]+)[\])]\s*$")
SUPPLEMENT = re.compile(r"\b(?:supplement(?:ary|al) (?:material|information|data|table|figure)|supporting information)", re.I)
PROCEDURE = re.compile(
    r"\b(?:we (?:measure[ds]?|evaluat\w*|test\w*|obtain\w*|report\w*|conduct\w*|assess\w*|"
    r"compar\w*|present\w*|reimplement\w*|follow\w*)|our (?:experiments?|results?|measurements?|evaluation)|"
    r"measured under|parameters of .{0,155} under simulated|device parameters of the different .{0,70}\s+solar cells|"
    r"summary of photovoltaic performance for champion devices|main results of multi-hop QA datasets|"
    r"our training complements retrieval augmentation|exhibited PCE\s*=)", re.I | re.S,
)
CITED = re.compile(r"\b(?:taken|copied|sourced|quoted|obtained|reported) (?:directly )?(?:from|by)\b", re.I)


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
    facts: dict[str, str] = Field(max_length=20)


class Reply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    results: list[Proposal] = Field(max_length=6)


def fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-").casefold().split())


def metric_label(text, domain):
    text = UNIT_TAIL.sub("", text.strip()).strip()
    for metric, aliases in METRICS[domain].items():
        match = re.fullmatch(rf"(?:{aliases})(?:\s*@\s*(\d+))?", text, re.I)
        if match and (not match[1] or domain == "rag"):
            return metric, int(match[1]) if match[1] else None
    return None


def relevant(text, domain):
    metric_terms = "|".join(METRICS[domain].values())
    return bool(re.search(rf"(?<!\w)(?:{metric_terms})(?!\w)", text, re.I) and re.search(r"\d", text))


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
            if not unit:
                # A reported score with an unspecified scale remains a raw value.
                return Value()
            scales[metric] = {"%": ("%", 1), "fraction": ("fraction", 1), "ratio": ("fraction", 1)}
    if unit not in scales.get(metric, {}):
        raise EvidenceError("unknown_unit_or_scale")
    normalized_unit, factor = scales[metric][unit]
    return Value(value=str(value * Decimal(factor)), unit=normalized_unit)


def _table_unit(cell):
    if cell.aggregation == "champion" and (record := RECORD.fullmatch(cell.value.text)):
        return record[1], UNIT_TAIL.search(cell.headers[-1].text)[1], None
    number = CELL.fullmatch(cell.value.text)
    header = UNIT_TAIL.search(cell.headers[-1].text)
    raw_unit = number[3] or (header[1] if header else "")
    if number[3] and header and _unit_key(number[3]) != _unit_key(header[1]):
        raise EvidenceError("conflicting_units")
    return number[1], raw_unit, number[2]


def _table_metric(cell, domain):
    for b in reversed(cell.headers):
        if not b.text.startswith("("):
            return metric_label(b.text, domain)
    return None


def _origin(text):
    if CITED.search(text):
        return "cited"
    if PROCEDURE.search(text):
        return "primary"
    return "unknown"


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
        result["device_label"] = row
        if cell.aggregation:
            result["aggregation"] = cell.aggregation
        direction = re.findall(r"\b(reverse|forward|average)\s+(?:J.?V\s+)?(?:scan|sweep)", " ".join([caption, *headers, row]), re.I)
        if len(set(map(str.lower, direction))) == 1:
            result["scan_direction"] = direction[0].lower()
        intensity = re.findall(rf"({_NUMBER})\s*mW\s*(?:/\s*cm[²2]|cm\s*[−⁻-]\s*[²2])", caption, re.I)
        if len(set(intensity)) == 1:
            result.update(light_intensity=intensity[0], intensity_unit="mW/cm²")
        spectrum = re.search(r"\bAM\s*1\.5\s*G\b", caption, re.I)
        if spectrum:
            result["spectrum"] = spectrum[0]
        if re.search(r"\bJ\s*[–-]\s*V\b", caption, re.I):
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
        "retrieval_method": "retrieval method", "retriever_model": "retriever(?: model)?",
        "generator_model": "generator(?: model)?", "dataset_name": "dataset",
        "task_type": "task", "evaluation_protocol": "evaluation protocol",
        "metric_definition": "metric definition", "top_k": r"(?:retrieval )?top[ _-]k",
    })
    if "top_k" in result and not re.fullmatch(r"\d+", result["top_k"]):
        result.pop("top_k")
    result["rag_variant"] = row
    if len(headers) == 3 and re.search(r"\bdatasets?\b", caption, re.I):
        if re.search(r"llama|qwen|gpt|mistral|gemini|t5|instruct|claude|generator|model", headers[0], re.I):
            result["generator_model"] = headers[0]
            result["dataset_name"] = headers[1]
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
        label = _table_metric(cell, domain)
        if label is None:
            continue
        try:
            raw_value, raw_unit, _ = _table_unit(cell)
        except EvidenceError:
            continue
        metric, k = label
        proposals.append(Proposal(
            cell=cell.value.block_id, row=[b.block_id for b in cell.row],
            headers=[b.block_id for b in cell.headers], block=cell.value.block_id, quote=cell.value.text,
            raw_value=raw_value, raw_unit=raw_unit,
            metric=f"{metric}@{k}" if k is not None else metric,
            origin=_origin(_support_caption(fragment)), facts={},
        ))
    return proposals


def make_prompt(fragment, domain):
    import json
    context = (
        "Solar keys include device_label, material_name_raw, formula_normalized, architecture, device_type, "
        "layer_order, layer_material, layer_function, deposition_method, annealing_temperature, "
        "measurement_type, scan_direction, spectrum, light_intensity, intensity_unit, aggregation, note."
        if domain == "solar" else
        "RAG keys include rag_variant, retrieval_method, retriever_model, generator_model, dataset_name, "
        "task_type, evaluation_protocol, metric_definition, top_k, note. Put metric K in metric as Name@K."
    )
    return (
        "Return JSON {results: [...]} with up to six actual scientific measurements supported by the fragment. "
        "Each result has cell (null for prose), row and headers (arrays of block IDs in source order), block "
        "(the value block ID), quote (exact full table cell or complete prose claim), raw_value (the exact "
        "number), raw_unit (explicit unit or empty), metric, origin (primary, cited, or unknown), and facts "
        "(a dictionary of context field to exact source wording). For table results use the given geometry. "
        "For prose return one unambiguous assertion with one metric, its object and measurement conditions. "
        "Omit unsupported claims and return an empty list if no claim can be grounded. "
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
            raise EvidenceError("wrong_table_value_or_unit")
        context.update(_table_context(cell, fragment, domain))
        geometry = [*cell.row, *cell.headers, source_block]
        if fragment.caption:
            geometry.extend(fragment.caption_support or (fragment.caption,))
        if spread:
            context["published_spread"] = spread
        origin_text = _support_caption(fragment)
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
    if fragment.cells and domain == "rag" and not {"rag_variant", "generator_model", "dataset_name"} <= context.keys():
        errors.append("rag_context_unresolved")
    try:
        normalized = normalize(proposal.raw_value, proposal.raw_unit, name, domain)
    except EvidenceError as exc:
        errors.append(str(exc))
    _facts(proposal, [origin_text, *(b.text for b in geometry)], domain, context, errors)
    if proposal.facts:
        context["proposed_facts"] = proposal.facts
    context["evidence_blocks"] = [
        {"block_id": b.block_id, "bbox": b.bbox, "text": b.text} for b in dict.fromkeys(geometry)
    ]
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
