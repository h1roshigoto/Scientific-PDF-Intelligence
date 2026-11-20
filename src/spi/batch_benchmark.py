"""Compare a saved batch run with participant dev_gold CSVs; extraction never imports this module."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .batch_evidence import fold


def _read(directory, name):
    with (directory / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _location(table, figure):
    return re.sub(r"\s*\(?cont\.?\)?$", "", fold(table or figure or ""))


def _number(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _unit(value):
    return fold(value or "").replace(" ", "").replace("^", "").replace("macm-2", "ma/cm2")


def _numeric_match(value, unit, reference_value, reference_unit, *, allow_scale_conversion=False):
    a, b = _number(value), _number(reference_value)
    left, right = _unit(unit), _unit(reference_unit)
    if a is None or b is None:
        return False
    if allow_scale_conversion and {left, right} == {"%", "fraction"}:
        return (a / 100 if left == "%" else a) == (b / 100 if right == "%" else b)
    return a == b and left == right


def _key(metric, k, source_page, source_table, source_figure):
    return (fold(metric), str(k or ""), str(source_page), _location(source_table, source_figure))


def _matches_context(candidate, reference, domain):
    if domain == "solar":
        device = fold(candidate.get("device_label") or "")
        label = fold(reference.get("device_label") or "")
        if re.fullmatch(r"[a-z]+\d+", device):
            # Curators may append fabrication details after the printed device code.
            label = re.sub(r"\s+\([^()]*\)$", "", label)
        return bool(device) and device == label
    fields = ("rag_variant", "generator_model", "dataset_name")
    return all(candidate.get(field) and fold(str(candidate[field])) == fold(reference.get(field) or "")
               for field in fields)


def compare(batch, directory):
    domain = batch["domain"]
    relations = {"solar": (("devices", "device_id"),),
                 "rag": (("configurations", "config_id"), ("datasets", "dataset_id"))}[domain]
    indexes = {key: {(r["publication_id"], r[key]): r for r in _read(directory, table)}
               for table, key in relations}
    pubs = {r["publication_id"]: r for r in _read(directory, "publications")}
    by_doc = defaultdict(list)
    for record in _read(directory, "results"):
        if record["extraction_status"] == "extracted":
            by_doc[record["publication_id"]].append(record)
    output = {}
    for doc in batch["documents"]:
        document = doc["document_id"]
        pid = document.split(":", 1)[1]
        refs = by_doc[pid]
        by_key = defaultdict(list)
        for ref in refs:
            context = {}
            for _, key in relations:
                context.update(indexes[key].get((pid, ref[key]), {}))
            by_key[_key(ref["metric_name"], ref.get("metric_k"), ref["source_page"],
                        ref["source_table"], ref["source_figure"])].append((ref, context))
        matched = set()
        counts = Counter()
        for step in doc.get("fragments", {}).values():
            counts["review_fragments"] += step["status"] == "review"
            for row in step.get("results", []):
                counts[row["extraction_status"]] += 1
                if row["extraction_status"] != "verified":
                    continue
                source = row["source"]
                possible = by_key[_key(row["metric"]["name"], row["metric"]["k"],
                                       source["page"], source["table"], source["figure"])]
                context = [(ref, ctx) for ref, ctx in possible if _matches_context(row["context"], ctx, domain)]
                counts["gold_metric_page_location"] += bool(possible)
                counts["gold_core_context"] += bool(context)
                raw = row["raw"]
                normalized = row["normalized"]
                counts["output_has_normalized_value"] += normalized["value"] is not None
                raw_matches = []
                normalized_matches = []
                quote_matches = []
                for ref, _ in context:
                    gold_quote = fold(ref["source_fragment"])
                    quote = fold(source["fragment"] or "")
                    if quote and quote in gold_quote:
                        quote_matches.append(ref)
                    if _numeric_match(raw["value"], raw["unit"], ref["raw_value"], ref["raw_unit"]):
                        raw_matches.append(ref)
                    if _numeric_match(normalized["value"], normalized["unit"], ref["normalized_value"],
                                      ref["normalized_unit"], allow_scale_conversion=True):
                        normalized_matches.append(ref)
                counts["gold_raw_value_unit_with_core_context"] += bool(raw_matches)
                counts["gold_normalized_value_unit_with_core_context"] += bool(normalized_matches)
                counts["gold_quote_with_core_context"] += bool(quote_matches)
                eligible = {ref["result_id"] for ref in raw_matches + normalized_matches
                            if (ref.get("result_origin") or None) == source.get("origin")
                            and (domain != "solar" or not (row["context"].get("aggregation") and ref.get("aggregation"))
                                 or row["context"]["aggregation"] == ref["aggregation"])}
                if len(eligible) == 1:
                    matched.update(eligible)
        output[document] = {
            "gold_scope": pubs.get(pid, {}).get("in_scope"), "batch_scope": doc["scope"]["status"],
            "technical_status": doc["status"], "pages": dict(Counter(p["read_status"] for p in doc.get("pages", []))),
            "gold_extracted_rows": len(refs), "gold_unique_core_matches": len(matched), **counts,
        }
    return {"domain": domain, "documents": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON written by spi.cli --report")
    parser.add_argument("gold", type=Path, help="Local dev_gold directory")
    args = parser.parse_args(argv)
    batches = json.loads(args.report.read_text(encoding="utf-8-sig"))
    print(json.dumps(compare(batches, args.gold), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
