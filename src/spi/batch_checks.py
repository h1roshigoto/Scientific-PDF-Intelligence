"""Check the compatibility of published measurements without changing them."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from .batch_evidence import fold
from .models import ValidationIssue


def quality_gate(in_scope, results, checkpoints):
    """Check scientific coverage for the selected document IDs and PDF hashes."""
    verified = Counter()
    needs_review = 0
    for result in results:
        metadata = result.extraction_metadata
        if metadata is None or in_scope.get(result.document_id) != metadata.pdf_sha256:
            continue
        if result.extraction_status == "verified":
            verified[result.document_id] += 1
        elif result.extraction_status == "needs_review":
            needs_review += 1
    layout_unresolved = pending = unresolved_cells = 0
    for document in checkpoints:
        if document["document_id"] not in in_scope or in_scope[document["document_id"]] != document.get("sha256"):
            continue
        layout_unresolved += len(document.get("layout_issues", ()))
        pending += document.get("pending_units", 0)
        for unit in document.get("fragments", {}).values():
            if unit.get("status") == "review":
                unresolved_cells += len({detail["source_block_id"] for detail in unit.get("review_details", ())
                                         if detail.get("source_block_id")})
                if not unit.get("results") and not unit.get("review_details"):
                    unresolved_cells += 1
    unresolved = layout_unresolved + unresolved_cells + pending
    missing = sorted(doc.split(":", 1)[1] for doc in in_scope.keys() - verified.keys())
    status = "NO-GO" if not verified else "PARTIAL" if missing or needs_review or unresolved else "GO"
    return {
        "status": status, "ready": status == "GO",
        "in_scope_publications": len(in_scope), "publications_with_verified_results": len(verified),
        "verified_results": sum(verified.values()), "needs_review": needs_review, "unresolved": unresolved,
        "layout_unresolved": layout_unresolved, "unresolved_cells_or_fragments": unresolved_cells,
        "pending_units": pending, "publications_without_verified_results": missing,
    }


def check_pce(values, intensity, *, comparable):
    if not comparable:
        return {"status": "not_evaluable", "reason": "measurement_context_incomplete_or_incompatible"}
    if not {"PCE", "Voc", "Jsc", "FF"} <= values.keys() or not intensity:
        return {"status": "not_evaluable", "reason": "missing_metric_or_irradiance"}
    try:
        pce, voc, jsc, ff = (Decimal(str(values[key])) for key in ("PCE", "Voc", "Jsc", "FF"))
        pin = Decimal(str(intensity))
        if not all(v.is_finite() for v in (pce, voc, jsc, ff, pin)) or min(pce, voc, jsc, ff, pin) <= 0 or ff > 1:
            raise InvalidOperation
        calculated = 100 * voc * jsc * ff / pin
        relative = abs(calculated - pce) / abs(pce)
    except (InvalidOperation, TypeError, ValueError):
        return {"status": "not_evaluable", "reason": "invalid_values"}
    return {"status": "pass" if relative <= Decimal(".03") else "fail",
            "published_pce_percent": str(pce), "calculated_pce_percent": str(calculated),
            "relative_difference": str(relative)}


def evidence_ref(result):
    return {"document_id": result.document_id, "page": result.source.page,
            "fragment": result.source.fragment, "table": result.source.table, "figure": result.source.figure}


def _issue(rule, rows, reason, **kwargs):
    return ValidationIssue(
        issue_id=f"{rows[0].result_id}:{rule}", domain=rows[0].domain, rule_id=rule,
        result_ids=[r.result_id for r in rows], evidence_refs=[evidence_ref(r) for r in rows],
        reason=reason, **kwargs,
    )


def validate_results(results):
    repeated = defaultdict(list)
    pce_sets = defaultdict(list)
    solar_sources = defaultdict(list)
    checks, issues = [], []
    for result in results:
        context = result.context
        if result.domain == "solar":
            identity = (context.get("device_label"), context.get("scan_direction"), context.get("raw_scan_direction"),
                        context.get("measurement_type"), context.get("spectrum"),
                        context.get("light_intensity"), context.get("aggregation"),
                        context.get("temperature"), context.get("stress_conditions"))
            if result.extraction_status == "verified" and result.source.origin == "primary":
                pce_sets[(*identity, result.source.page, result.source.table, result.source.row)].append(result)
                if context.get("device_label") and context.get("aggregation") == "champion":
                    solar_sources[(context["device_label"], result.metric.name, context.get("scan_direction"),
                                   context.get("raw_scan_direction"))].append(result)
        else:
            identity = tuple(context.get(field) for field in (
                "rag_variant", "dataset_name", "generator_model", "retriever_model",
                "evaluation_protocol", "top_k", "task_type",
            ))
        if identity[0] and (result.domain == "solar" or all(identity[:3])):
            repeated[(*identity, result.source.origin, result.metric.name, result.metric.k)].append(result)

    for rows in pce_sets.values():
        if not any(r.metric.name == "PCE" for r in rows):
            continue
        expected = {"PCE": "%", "Voc": "V", "Jsc": "mA/cm²", "FF": "fraction"}
        by_metric = defaultdict(list)
        for result in rows:
            if result.metric.name in expected and result.normalized.unit == expected[result.metric.name]:
                by_metric[result.metric.name].append(result.normalized.value)
        values = {key: records[0] for key, records in by_metric.items() if len(records) == 1}
        ctx = rows[0].context
        # A single explicit J-V condition and the same physical row exclude cross-device joins.
        comparable = bool(ctx.get("measurement_type") == "J-V" and ctx.get("scan_direction")
                          and ctx.get("aggregation") in (None, "champion")
                          and ctx.get("intensity_unit") == "mW/cm²" and rows[0].source.table)
        check = check_pce(values, ctx.get("light_intensity"), comparable=comparable)
        checks.append({**check, "result_ids": [r.result_id for r in rows]})
        if check["status"] == "fail":
            issues.append(_issue("solar_pce_mismatch", rows, "Relative difference exceeds 3%",
                                 observed=check["published_pce_percent"], expected=check["calculated_pce_percent"]))

    for rows in repeated.values():
        # Identical numbers alone are insufficient to mark a duplicate. Retain every source.
        locations = {(r.source.table, r.source.figure, r.source.page) for r in rows}
        if len(locations) < 2:
            continue
        values = {(r.normalized.value or r.raw.value, fold(r.normalized.unit or r.raw.unit or "")) for r in rows}
        if len(values) == 1:
            issues.append(_issue("possible_duplicate", rows, "Matching reported values need comparison of source contexts"))
        elif all(r.extraction_status == "verified" for r in rows):
            issues.append(_issue("possible_source_discrepancy", rows,
                                 "Different published values for apparently similar contexts need review"))
    for rows in solar_sources.values():
        tables = [r for r in rows if r.source.table]
        figures = [r for r in rows if r.source.figure]
        if any(t.normalized.unit == f.normalized.unit and t.normalized.value != f.normalized.value
               for t in tables for f in figures):
            issues.append(_issue("table_figure_difference", tables + figures,
                                 "Table and figure report different values; check the measurement conditions"))
    return checks, issues
