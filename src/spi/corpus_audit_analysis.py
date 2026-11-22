"""Scientific and processing diagnoses grounded in a captured Store."""

from collections import Counter, defaultdict
import json
import re

from .batch_checks import quality_gate
from .batch_evidence import MODEL_PLAN_VERSION, PROFILES, SCHEMA_VERSIONS
from .batch_pdf import LAYOUT_VERSION
from .csv_schema import load_schema
from .models import ResultRecord, ValidationIssue
from .review_scope import DISPUTED, TRACKS

APPROVED_IDS = ("solar:P002", "solar:P006", "rag:P001", "rag:P003")
FRAGMENT_STATES = ("pending", "review", "done", "failed")
RESULT_STATES = ("verified", "needs_review", "extracted")
CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{0,100}")
RULES = {
    # category, action type, explanation, resolution
    "CHECKPOINT_STALE": ("technical", "INVESTIGATE_UNKNOWN", "Checkpoint SHA differs from the original.",
                         "Locate a checkpoint for the current SHA before scheduling any resume."),
    "CHECKPOINT_MISSING": ("technical", "INVESTIGATE_UNKNOWN", "No checkpoint was recorded.",
                           "Find the missing run or schedule a separate processing pass."),
    "PDF_PARSE_RETRY": ("technical", "RETRY_PDF_PARSING", "Checkpoint requests a parser retry.",
                        "Inspect the saved page error and retry parsing in a separate run."),
    "OCR_REQUIRED": ("technical", "OCR_REQUIRED", "Checkpoint recorded a page requiring OCR.",
                     "Review the listed PDF page and arrange OCR with page provenance."),
    "MODEL_CONFIGURATION_ERROR": ("technical", "RESUME_MODEL_EXTRACTION", "A model config or OAuth error was saved.",
                                  "Resolve the recorded setup error before resuming model extraction."),
    "MODEL_REQUEST_BUDGET": ("technical", "RESUME_MODEL_EXTRACTION", "There is a persisted budget-exhausted error.",
                             "Allocate a new request budget for the still-pending model work."),
    "CHECKPOINT_ERRORS": ("technical", "INVESTIGATE_UNKNOWN", "The checkpoint recorded a processing error.",
                          "Investigate the saved error category and page before resuming."),
    "CHECKPOINT_NOT_COMPLETE": ("technical", "INVESTIGATE_UNKNOWN", "The checkpoint is still running or failed.",
                                "Check whether the saved run was interrupted or failed."),
    "LAYOUT_ISSUES": ("technical", "FIX_EXTRACTION_LOGIC", "The parser left table layout unresolved.",
                      "Compare the saved anchor with the PDF table, then assess a layout rule fix."),
    "FRAGMENTS_PENDING": ("technical", "RESUME_MODEL_EXTRACTION", "The checkpoint has unfinished scheduled units.",
                          "Evaluate resume eligibility and complete the scheduled extraction work."),
    "FRAGMENTS_NEED_REVIEW": ("science", "HUMAN_SCIENTIFIC_REVIEW", "A fragment remains in the review state.",
                              "Inspect the source and adjudicate the saved review evidence."),
    "SCIENTIFIC_CONTEXT_INCOMPLETE": ("science", "HUMAN_SCIENTIFIC_REVIEW",
                                     "A result has explicitly unconfirmed scientific context.",
                                     "Resolve the listed context fields against the PDF and its cited sources."),
    "RESULTS_NEED_REVIEW": ("science", "HUMAN_SCIENTIFIC_REVIEW", "Current results are explicitly marked needs_review.",
                            "Review the current result against its exact source, units and scientific context."),
    "NO_EXTRACTED_RESULTS": ("science", "HUMAN_SCIENTIFIC_REVIEW", "The checkpoint produced no result records.",
                             "Check unresolved source evidence for eligible measurements and document the finding."),
    "NO_VERIFIED_RESULTS": ("science", "HUMAN_SCIENTIFIC_REVIEW", "The included publication has no verified current result.",
                            "Trace current review records or establish why eligible measurements lack a verified record."),
    "SCIENTIFIC_EVIDENCE_UNKNOWN": ("science", "INVESTIGATE_UNKNOWN", "Required checkpoint evidence is missing.",
                                    "Recover the saved fragment, layout and pending inventories for this PDF SHA."),
    "VALIDATION_ERROR": ("science", "HUMAN_SCIENTIFIC_REVIEW", "A scientific validation issue is an error.",
                         "Adjudicate the recorded rule against its evidence before release."),
    "SCOPE_NOT_FINAL": ("scope", "HUMAN_SCOPE_REVIEW", "The validated review has no final decision for this PDF SHA.",
                        "Have a reviewer document a final decision with PDF pages in the approval worksheet."),
    "SCOPE_CHANGED_AFTER_PROCESSING": ("scope", "INVESTIGATE_UNKNOWN",
                                       "Batch-time scope and current verified review status differ.",
                                       "Account for the final scope when planning processing or exclusion."),
    "UNKNOWN_PARTIAL_CAUSE": ("technical", "INVESTIGATE_UNKNOWN", "Saved fields do not prove why the run is partial.",
                              "Inspect the original run diagnostics and checkpoint version."),
    "METADATA_UNCONFIRMED": ("submission", "FIX_PUBLICATION_METADATA",
                             "Bibliographic fields lack a confirmed source in the available Store.",
                             "Verify title, year, DOI and authors against publication front matter or publisher record."),
    "METADATA_NOT_EXPORTED": ("submission", "FIX_EXPORT_MAPPING", "The exporter omits fields present in the Store.",
                              "After bibliographic verification, map the supported publication columns."),
    "ENTITY_LINKAGE_CANDIDATE": ("submission", "FIX_ENTITY_LINKAGE",
                                 "Multiple records share an evidence-backed entity signature.",
                                 "Review the entity signatures and evidence before considering a shared ID."),
    "ISSUE_NOT_PROJECTED": ("submission", "FIX_EXPORT_MAPPING",
                            "An issue can be omitted by the current schema or verified-only mapping.",
                            "Review the mapping or preserve a separately traceable scientific issue record."),
}
BATCH_CAUSES = {
    "PDF_PARSE_RETRY", "OCR_REQUIRED", "LAYOUT_ISSUES", "FRAGMENTS_PENDING",
    "FRAGMENTS_NEED_REVIEW", "NO_EXTRACTED_RESULTS", "UNKNOWN_PARTIAL_CAUSE",
}


def code(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else None


def identity_index(store, docs):
    checkpoints = {}
    for track in TRACKS:
        for checkpoint in store.batch_documents(track):
            key = checkpoint["document_id"]
            if key in checkpoints or key not in docs or track != docs[key]["domain"] or checkpoint["domain"] != track:
                raise ValueError("Duplicate or inconsistent checkpoint ownership")
            checkpoints[key] = checkpoint
    owners, accepted, excluded = {}, {}, defaultdict(Counter)

    def add(raw, checkpoint_owner=None):
        result = ResultRecord.model_validate(raw)
        key = result.document_id
        if key not in docs or result.domain != docs[key]["domain"] or checkpoint_owner not in (None, key):
            raise ValueError("Result is attached to the wrong document")
        if owners.setdefault(result.result_id, key) != key:
            raise ValueError("A result ID is assigned to two documents")
        # Store precedence is results.json followed by current checkpoint fragments.
        # Rows without a current digest are never eligible for the audit.
        metadata = result.extraction_metadata
        if metadata is None or metadata.pdf_sha256 != docs[key]["sha256"]:
            excluded[key]["missing_sha" if metadata is None else "stale_sha"] += 1
            return
        if result.source.page > docs[key]["page_count"]:
            raise ValueError(f"{key}: result evidence page is outside the current PDF")
        accepted[result.result_id] = result

    file_rows = store.read("results.json")
    if len({raw["result_id"] for raw in file_rows}) != len(file_rows):
        raise ValueError("Duplicate result IDs in results.json")
    for raw in file_rows:
        add(raw)
    for key, checkpoint in checkpoints.items():
        if checkpoint.get("sha256") != docs[key]["sha256"]:
            excluded[key]["stale_checkpoint_rows"] += sum(
                len(fragment.get("results", ())) for fragment in checkpoint.get("fragments", {}).values()
            )
            continue
        for fragment in checkpoint.get("fragments", {}).values():
            for raw in fragment.get("results", ()):
                add(raw, checkpoint_owner=key)
    by_doc = defaultdict(list)
    for result_id in sorted(accepted):
        by_doc[accepted[result_id].document_id].append(accepted[result_id])
    return checkpoints, by_doc, accepted, excluded


def issue_diagnostics(store, docs, results, scopes, checkpoints):
    issues = list(store.issue_entries())
    by_doc = defaultdict(list)
    records = []
    excluded_contract = {track: "issues" in load_schema(track).excluded_tables for track in TRACKS}
    for owner, issue in issues:
        linked = [results[rid] for rid in sorted(set(issue.result_ids)) if rid in results]
        owners = {r.document_id for r in linked}
        refs = []
        for ref in issue.evidence_refs:
            key = ref.get("document_id")
            hit = results.get(ref.get("result_id"))
            bound = key or (hit.document_id if hit else None) or owner
            if bound in docs:
                owners.add(bound)
            refs.append({"document_id": bound, "result_id": ref.get("result_id"),
                         "source_page": ref.get("page"), "source_block_id": ref.get("source_block_id"),
                         "source_table": ref.get("table"), "source_figure": ref.get("figure")})
        if owner:
            owners.add(owner)
        exported = {r.document_id for r in linked if r.extraction_status == "verified"
                    and r.document_id in scopes and scopes[r.document_id].status == "in_scope"}
        excluded = excluded_contract[issue.domain]
        row = {
            "issue_id": issue.issue_id, "domain": issue.domain, "rule_id": issue.rule_id,
            "severity": issue.severity, "checkpoint_owner": owner, "document_ids": sorted(owners),
            "current_verified_result_ids": [r.result_id for r in linked if r.extraction_status == "verified"],
            "current_needs_review_result_ids": [r.result_id for r in linked if r.extraction_status == "needs_review"],
            "unresolved_result_ids": sorted(set(issue.result_ids) - results.keys()),
            "source_evidence": refs, "contract_excludes_table": excluded,
            "potential_export_documents": sorted(exported) if not excluded else [],
            "potentially_omitted": excluded or not exported,
            "documents_without_export_link": sorted(owners - (exported if not excluded else set())),
        }
        records.append(row)
        for key in owners:
            by_doc[key].append(row)
    stale_records = [
        {"document_id": key, "checkpoint_sha256": checkpoint.get("sha256"), "issue_id": issue.issue_id,
         "rule_id": issue.rule_id, "severity": issue.severity}
        for key, checkpoint in sorted(checkpoints.items()) if checkpoint.get("sha256") != docs[key]["sha256"]
        for issue in map(ValidationIssue.model_validate, checkpoint.get("science_issues", ()))
    ]
    return issues, by_doc, {
        "total_in_store": len(records) + len(stale_records), "current": len(records),
        "stale_checkpoint": len(stale_records), "stale_records": stale_records,
        "linked_to_verified": sum(bool(r["current_verified_result_ids"]) for r in records),
        "linked_to_needs_review": sum(bool(r["current_needs_review_result_ids"]) for r in records),
        "potentially_omitted": sum(r["potentially_omitted"] for r in records),
        "partially_represented": sum(bool(r["documents_without_export_link"]) and not r["potentially_omitted"]
                                     for r in records),
        "unattributed": sum(not r["document_ids"] for r in records),
        "coverage": "full_store_snapshot", "contract_excludes_issues": excluded_contract,
        "by_severity": dict(Counter(r["severity"] for r in records)),
        "records": sorted(records, key=lambda r: (r["domain"], r["issue_id"], r["checkpoint_owner"] or "")),
    }


ENTITY_SIGNATURES = {
    "solar": {
        "devices": ("device_label", "device_role", "device_type", "architecture", "active_area", "area_unit",
                    "area_definition", "substrate_type", "encapsulation", "material_name_raw", "layers"),
        "measurements": ("device_label", "measurement_type", "scan_direction", "raw_scan_direction", "light_intensity",
                         "intensity_unit", "spectrum", "temperature", "scan_rate", "mask_used", "certified",
                         "certification_lab", "stability_protocol", "stress_conditions"),
    },
    "rag": {
        "configurations": ("retrieval_method", "generator_model", "retriever_model", "top_k", "rag_variant",
                           "reranker_model", "chunk_size", "prompt_strategy", "generator_version",
                           "embedding_model", "embedding_version", "chunking_method", "chunk_overlap",
                           "chunk_unit", "index_type", "vector_store", "context_strategy"),
        "datasets": ("dataset_name", "dataset_version", "data_split", "language"),
        "experiments": ("dataset_name", "task_type", "generator_model", "evaluation_protocol",
                        "experiment_type", "retrieval_method", "top_k", "baseline_name", "hardware", "data_split"),
    },
}
REQUIRED_SIGNATURE = {
    "devices": ("device_label",), "measurements": ("device_label", "measurement_type"),
    "configurations": ("retrieval_method", "generator_model"), "datasets": ("dataset_name",),
    "experiments": ("dataset_name", "task_type", "generator_model"),
}


def result_ref(result):
    return {"result_id": result.result_id, "status": result.extraction_status, "source_page": result.source.page,
            "source_block_id": result.extraction_metadata.source_block_id,
            "origin": result.source.origin, "metric_k": result.metric.k, "top_k": result.context.get("top_k")}


def entity_clusters(domain, results):
    clusters = []
    for entity, fields in ENTITY_SIGNATURES[domain].items():
        groups = defaultdict(list)
        for result in results:
            signature = {f: result.context[f] for f in fields if result.context.get(f) not in (None, "")}
            if result.source.origin is not None:
                signature["origin"] = result.source.origin
            if result.source.cited_publication_id is not None:
                signature["cited_publication_id"] = result.source.cited_publication_id
            if all(field in signature for field in REQUIRED_SIGNATURE[entity]):
                groups[json.dumps(signature, sort_keys=True)].append(result)
        for signature, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            refs = [result_ref(r) for r in members]
            same_page = len({r.source.page for r in members}) == 1
            confidence = "medium" if same_page and all(r.extraction_status == "verified" for r in members) else "low"
            clusters.append({"entity": entity, "signature": json.loads(signature), "confidence": confidence,
                             "candidate_projection_rows": sum(r.extraction_status == "verified" for r in members),
                             "evidence": refs})
    return clusters


def publication_metadata(doc, sheet, pdf_metadata):
    fields = {}
    for field, aliases in {"title": ("title",), "year": ("year",), "DOI": ("DOI", "doi"),
                           "authors": ("authors", "author")}.items():
        stored = next((doc[a] for a in aliases if doc.get(a)), None)
        candidate = next((pdf_metadata[a] for a in aliases if pdf_metadata.get(a)), None)
        source = "embedded_pdf_metadata" if candidate else None
        if not candidate and field == "title" and sheet.get("title"):
            candidate, source = sheet["title"], "approval_worksheet_title"
        fields[field] = {"store_value": stored, "store_status": "available" if stored else "missing",
                         "export_mapping": "mapped_from_store" if stored else "unmapped",
                         "candidate_value": candidate, "candidate_source": source,
                         "confirmation": "source_unconfirmed" if stored or candidate else "unknown"}
    return fields


def fragment_evidence(fragment, detail=None):
    detail = detail or {}
    ids = {r.get("extraction_metadata", {}).get("source_block_id")
           for r in fragment.get("results", ()) if r.get("extraction_metadata")}
    review_source = fragment.get("review_source") or {}
    if not any(ids):
        ids = {b.get("block_id") for b in review_source.get("blocks", ())}
    ids.discard(None)
    return {"source_block_id": detail.get("source_block_id") or (next(iter(ids)) if len(ids) == 1 else None),
            "source_page": detail.get("page", fragment.get("page"))}


def diagnose_checkpoint(c, add):
    unresolved = []
    for page in c.get("pages_needing_ocr", ()):
        add("OCR_REQUIRED", "pages_needing_ocr", page, source_page=page)
    for index, issue in enumerate(c.get("layout_issues", ())):
        evidence = {"source_block_id": issue.get("source_block_id"), "source_page": issue.get("page")}
        add("LAYOUT_ISSUES", f"layout_issues[{index}]", code(issue.get("reason")), **evidence)
        unresolved.append({"kind": "layout", "count": 1, "fragment_id": None,
                           "reason": code(issue.get("reason")), **evidence})
    if c.get("retry_parse"):
        failed_pages = [p for p in c.get("pages", ()) if p.get("error") or p.get("analysis_error")]
        for page in failed_pages or [{}]:
            field = "analysis_error" if page.get("analysis_error") else "error"
            add("PDF_PARSE_RETRY", f"pages.{field}" if page else "retry_parse",
                error_category(page[field]) if page else True, source_page=page.get("page"))
    saved_pending = []
    pending_action = {"local": "RETRY_PDF_PARSING", "gigachat": "RESUME_MODEL_EXTRACTION"}.get(
        c.get("mode"), "INVESTIGATE_UNKNOWN"
    )
    for fid, fragment in sorted(c.get("fragments", {}).items()):
        evidence = fragment_evidence(fragment)
        if fragment["status"] == "pending":
            saved_pending.append({"fragment_id": fid, **evidence})
            add("FRAGMENTS_PENDING", "fragments.status", "pending", fid, action_type=pending_action, **evidence)
        if fragment["status"] == "review":
            details = fragment.get("review_details", ())
            for detail in details or [{}]:
                pointer = fragment_evidence(fragment, detail)
                value = code(detail.get("code")) or sorted(filter(None, map(code, fragment.get("issues", ()))))
                add("FRAGMENTS_NEED_REVIEW", "fragments.review_details" if detail else "fragments.status",
                    value or "review", fid, **pointer)
            seen = set()
            for detail in details:
                block = detail.get("source_block_id")
                if block and block not in seen:
                    seen.add(block)
                    unresolved.append({"kind": "review_source_block", "count": 1, "fragment_id": fid,
                                       "reason": code(detail.get("code")), **fragment_evidence(fragment, detail)})
            if not fragment.get("results") and not details:
                unresolved.append({"kind": "review_fragment", "count": 1, "fragment_id": fid,
                                   "reason": sorted(filter(None, map(code, fragment.get("issues", ())))),
                                   **evidence})
        if fragment.get("error"):
            add_saved_error(add, fragment["error"], "fragments.error", fid, **evidence)
        elif fragment["status"] == "failed":
            add("CHECKPOINT_ERRORS", "fragments.status", "failed", fid, **evidence)
    if c.get("pending_units"):
        add("FRAGMENTS_PENDING", "pending_units", c["pending_units"], action_type=pending_action)
        unresolved.append({"kind": "pending_units", "count": c["pending_units"], "fragment_id": None,
                           "saved_pending_records": saved_pending, "source_block_id": None, "source_page": None})
    for error in c.get("errors", ()):
        add_saved_error(add, error.get("reason"), "errors.reason", source_page=error.get("page"))
    if c.get("status") in {"running", "failed"}:
        add("CHECKPOINT_NOT_COMPLETE", "status", c["status"])
    if (c.get("status") == "partial" and "fragments" in c
            and not any(f.get("results") for f in c["fragments"].values())):
        add("NO_EXTRACTED_RESULTS", "fragments.results", 0)
    return unresolved


def error_category(error):
    if isinstance(error, str):
        return code(error) or code(error.partition(": ")[0]) or "saved error text omitted"
    return "UNKNOWN"


def add_saved_error(add, error, field, fid=None, **source):
    kind = code(error)
    add("CHECKPOINT_ERRORS", field, error_category(error), fid, **source)
    if kind == "request_budget_exhausted":
        add("MODEL_REQUEST_BUDGET", field, kind, fid, **source)
    elif kind and kind.startswith(("config:", "oauth:")):
        add("MODEL_CONFIGURATION_ERROR", field, kind, fid, **source)


def valid_checkpoint(key, checkpoint, doc):
    if checkpoint.get("status") not in {"running", "processed", "partial", "failed"}:
        raise ValueError(f"{key}: unrecognized checkpoint status")
    if checkpoint.get("page_count") not in (None, doc["page_count"]):
        raise ValueError(f"{key}: checkpoint page count differs from current registry")
    state = checkpoint.get("scope_status_at_run")
    if state not in (None, "needs_review", "in_scope", "out_of_scope"):
        raise ValueError(f"{key}: unrecognized scope-at-run status")
    for field in ("pending_units", "planned_fragments"):
        if field in checkpoint and (type(checkpoint[field]) is not int or checkpoint[field] < 0):
            raise ValueError(f"{key}: invalid {field}")
    pages = checkpoint.get("pages_needing_ocr", ())
    if (any(type(p) is not int or not 1 <= p <= doc["page_count"] for p in pages)
            or len(set(pages)) != len(pages)):
        raise ValueError(f"{key}: OCR page list contains duplicates or invalid physical pages")
    if any(f["status"] not in FRAGMENT_STATES for f in checkpoint.get("fragments", {}).values()):
        raise ValueError(f"{key}: unrecognized fragment status")
    fragments = checkpoint.get("fragments", {})
    pointers = [*checkpoint.get("layout_issues", ())]
    for fragment in fragments.values():
        pointers.extend(fragment.get("review_details", ()))
        if fragment.get("review_source"):
            pointers.append(fragment["review_source"])
    for pointer in pointers:
        if pointer.get("pdf_sha256", doc["sha256"]) != doc["sha256"]:
            raise ValueError(f"{key}: checkpoint evidence belongs to another PDF version")

def review_page_hints(value, page_count):
    if not value:
        return None
    try:
        pages = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(pages, list) and all(type(page) is int and 1 <= page <= page_count for page in pages):
        return pages
    return None


def diagnose_document(doc, checkpoint, results, issues, decision, saved_scope, queue, sheet, pdf_metadata, excluded):
    key, digest = doc["document_id"], doc["sha256"]
    current = checkpoint is not None and checkpoint.get("sha256") == digest
    c = checkpoint if current else {}
    scope = decision.status if decision else "needs_review"
    scope_source = ("approval_worksheet" if sheet else "previous_confirmed_baseline") if decision else (
        "store_pending_and_review_queue" if saved_scope else "review_queue")
    pending_reason = None
    if not decision:
        pending_reason = "Paired RAG criterion awaits both approvals" if (
            key in DISPUTED and sheet.get("human_approved", "").strip().upper() == "TRUE"
        ) else "No final human approval for this PDF SHA"
    causes = []

    def add(cause, field, value, fid=None, source_block_id=None, source_page=None, action_type=None,
            result_id=None):
        category, action, explanation, resolution = RULES[cause]
        is_partial = c.get("status") == "partial" and cause in BATCH_CAUSES
        if cause == "FRAGMENTS_PENDING" and not c.get("pending_units"):
            is_partial = False
        if c.get("status") == "partial" and c.get("pending_units") and cause in {
            "MODEL_CONFIGURATION_ERROR", "MODEL_REQUEST_BUDGET",
        }:
            is_partial = True
        causes.append({
            "document_id": key, "pdf_sha256": digest,
            "checkpoint_revision": checkpoint.get("revision") if cause == "CHECKPOINT_STALE" else c.get("revision"),
            "cause_code": cause, "category": category, "partial_condition": is_partial,
            "severity": "error" if cause in {"VALIDATION_ERROR", "CHECKPOINT_ERRORS", "CHECKPOINT_STALE"}
            else "warning",
            "fragment_id": fid, "source_block_id": source_block_id, "source_page": source_page, "result_id": result_id,
            "evidence_field": field, "evidence_value": value, "explanation": explanation,
            "suggested_resolution": resolution, "action_type": action_type or action,
        })

    if not decision:
        add("SCOPE_NOT_FINAL", "scope_human_approved", False)
    if not checkpoint:
        add("CHECKPOINT_MISSING", "batch.checkpoint", None)
    elif not current:
        add("CHECKPOINT_STALE", "batch.checkpoint.sha256", checkpoint.get("sha256"))
    if current:
        valid_checkpoint(key, c, doc)
        unresolved = diagnose_checkpoint(c, add)
        if c.get("status") == "partial" and not any(x["partial_condition"] for x in causes):
            add("UNKNOWN_PARTIAL_CAUSE", "status", "partial")
        if c.get("scope_status_at_run") and c["scope_status_at_run"] != scope:
            add("SCOPE_CHANGED_AFTER_PROCESSING", "scope_status_at_run/current_scope",
                [c["scope_status_at_run"], scope])
    else:
        unresolved = None

    result_counts = Counter(r.extraction_status for r in results)
    if scope == "in_scope" and not result_counts["verified"]:
        add("NO_VERIFIED_RESULTS", "current_result_statuses.verified", 0)
    context_backlog = []
    for result in results:
        ref = result_ref(result)
        pointer = {k: ref[k] for k in ("source_block_id", "source_page", "result_id")}
        if result.extraction_status == "needs_review":
            add("RESULTS_NEED_REVIEW", "results.extraction_status", "needs_review", **pointer)
        context = result.context.get("unconfirmed_context")
        if isinstance(context, (list, dict)):
            fields = sorted(filter(None, map(code, context)))
            if fields:
                add("SCIENTIFIC_CONTEXT_INCOMPLETE", "results.context.unconfirmed_context", fields, **pointer)
                context_backlog.append({**ref, "unconfirmed_context": fields})
    issue_counts = Counter(issue["severity"] for issue in issues)
    for issue in issues:
        ref = next((r for r in issue["source_evidence"] if r["document_id"] == key), {})
        pointer = {name: ref.get(name) for name in ("source_block_id", "source_page")}
        if issue["severity"] == "error":
            add("VALIDATION_ERROR", "validation_issues.issue_id", issue["issue_id"], **pointer)
        if key in issue["documents_without_export_link"]:
            add("ISSUE_NOT_PROJECTED", "validation_issues.issue_id", issue["issue_id"], **pointer)

    fields = publication_metadata(doc, sheet, pdf_metadata)
    unconfirmed = [name for name, field in fields.items() if field["confirmation"] != "confirmed"]
    ignored = [name for name, field in fields.items() if field["export_mapping"] == "ignored_by_exporter"]
    if unconfirmed:
        add("METADATA_UNCONFIRMED", "publication_metadata.fields", unconfirmed)
    if ignored:
        add("METADATA_NOT_EXPORTED", "publication_metadata.fields", ignored)
    clusters = entity_clusters(doc["domain"], results)
    if clusters:
        add("ENTITY_LINKAGE_CANDIDATE", "entity_linkage.candidate_clusters", len(clusters))

    pending = c.get("pending_units")
    ocr = sorted(c["pages_needing_ocr"]) if "pages_needing_ocr" in c else None
    fragments = Counter(f["status"] for f in c.get("fragments", {}).values())
    partial = sorted({item["cause_code"] for item in causes if item["partial_condition"]})
    expected_unresolved = quality_gate({key: digest}, results, [c] if current else [])["unresolved"]
    if unresolved is not None and sum(entry["count"] for entry in unresolved) != expected_unresolved:
        raise ValueError(f"{key}: unresolved evidence does not reconcile to the scientific gate")
    missing_gate_fields = {"layout_issues", "pending_units", "fragments"} - c.keys()
    gate_inventory_complete = current and not missing_gate_fields
    if current and scope == "in_scope" and missing_gate_fields:
        add("SCIENTIFIC_EVIDENCE_UNKNOWN", "checkpoint.fields", sorted(missing_gate_fields))
    batch_reasons = {item["cause_code"] for item in causes}
    relevant = batch_reasons - {"SCOPE_CHANGED_AFTER_PROCESSING"}
    if scope == "out_of_scope":
        relevant = {name for name in relevant if RULES[name][0] == "submission"}
    ranking = ("SCOPE_NOT_FINAL", "CHECKPOINT_STALE", "CHECKPOINT_MISSING", "PDF_PARSE_RETRY", "OCR_REQUIRED",
               "MODEL_CONFIGURATION_ERROR", "MODEL_REQUEST_BUDGET", "CHECKPOINT_ERRORS", "CHECKPOINT_NOT_COMPLETE",
               "LAYOUT_ISSUES", "FRAGMENTS_PENDING", "VALIDATION_ERROR", "NO_VERIFIED_RESULTS",
               "NO_EXTRACTED_RESULTS", "RESULTS_NEED_REVIEW", "FRAGMENTS_NEED_REVIEW", "UNKNOWN_PARTIAL_CAUSE")
    primary = next((name for name in (*ranking, *RULES) if name in relevant), None)
    gate_blockers = []
    if scope == "needs_review":
        gate_blockers.append("SCOPE_NOT_FINAL")
    if scope == "in_scope":
        if (c.get("status") != "processed" or ocr != [] or c.get("retry_parse") or c.get("errors")):
            gate_blockers.append("INCOMPLETE_CHECKPOINT")
        if not result_counts["verified"]:
            gate_blockers.append("NO_VERIFIED_RESULTS")
        if result_counts["needs_review"]:
            gate_blockers.append("RESULTS_NEED_REVIEW")
        if not gate_inventory_complete:
            gate_blockers.append("SCIENTIFIC_EVIDENCE_UNKNOWN")
        if expected_unresolved:
            gate_blockers.append("SCIENTIFIC_UNRESOLVED")
        if issue_counts["error"]:
            gate_blockers.append("VALIDATION_ERROR")
    if scope == "out_of_scope":
        resume = "excluded_after_scope_review"
    elif not current or c.get("status") not in {"processed", "partial"}:
        resume = "no_complete_resume_checkpoint"
    else:
        mode = c.get("mode")
        revision = f"{digest}|{LAYOUT_VERSION}|{PROFILES[doc['domain']]}|{SCHEMA_VERSIONS[doc['domain']]}|{mode}"
        if (mode not in {"local", "gigachat"} or c.get("revision") != revision
                or (mode == "gigachat" and c.get("model_plan_version") != MODEL_PLAN_VERSION)):
            resume = "saved_plan_or_revision_differs_from_current_code"
        elif pending or fragments["pending"] or c.get("retry_parse"):
            resume = "saved_pending_work_or_parse_retry"
        elif c.get("scope_status_at_run") != (saved_scope.status if saved_scope else "needs_review"):
            resume = "store_scope_changed_since_checkpoint"
        else:
            resume = "same_mode_and_path_can_skip_even_if_partial"
    record = {
        "document_id": key, "domain": doc["domain"], "publication_id": doc["publication_id"],
        "pdf_sha256": digest, "pdf_pages": doc["page_count"], "scope_status": scope,
        "scope_decision_source": scope_source, "scope_human_approved": decision is not None,
        "scope_pending_reason": pending_reason, "store_scope_status": saved_scope.status if saved_scope else None,
        "scope_evidence_pages": decision.source_pages if decision else [],
        "scope_queue": {"review_start_pages": review_page_hints(queue.get("review_start_pages"), doc["page_count"]),
                        "worksheet_review_pages": review_page_hints(sheet.get("review_pages"), doc["page_count"]),
                        "evidence_origin": queue.get("evidence_origin") or None} if queue else None,
        "checkpoint_currency": "current" if current else "stale" if checkpoint is not None else "missing",
        "stale_checkpoint": {k: checkpoint.get(k) for k in ("sha256", "revision", "status")}
        if checkpoint is not None and not current else None,
        "checkpoint_status": c.get("status"), "checkpoint_revision": c.get("revision"),
        "processing_mode": c.get("mode"), "extraction_coverage": c.get("extraction_coverage"),
        "scope_status_at_run": c.get("scope_status_at_run"), "planned_fragments": c.get("planned_fragments"),
        "resume_observation": resume,
        "total_fragment_records": sum(fragments.values()) if "fragments" in c else None,
        **{f"{state}_fragments": fragments[state] if "fragments" in c else None for state in FRAGMENT_STATES},
        "pending_units": pending, "retry_parse": c.get("retry_parse"),
        "layout_issue_count": len(c["layout_issues"]) if "layout_issues" in c else None,
        "ocr_pending_pages_count": len(ocr) if ocr is not None else None, "ocr_pending_pages_json": ocr,
        "checkpoint_error_count": len(c["errors"]) if "errors" in c else None,
        "result_count": len(results), **{f"{status}_count": result_counts[status] for status in RESULT_STATES},
        "absent_count": None, "present_not_extractable_count": None,
        "excluded_result_rows": dict(excluded), "results": [result_ref(r) for r in results],
        "context_review": context_backlog, "validation_error_count": issue_counts["error"],
        "validation_warning_count": issue_counts["warning"], "validation_info_count": issue_counts["info"],
        "validation_issue_ids": sorted(issue["issue_id"] for issue in issues),
        "scientific_unresolved_count": expected_unresolved if gate_inventory_complete else None,
        "unresolved_items": unresolved, "partial_causes_json": partial,
        "diagnosed_causes": sorted(batch_reasons), "primary_blocker": primary,
        "recommended_action": RULES[primary][3] if primary else None,
        "confidence_of_diagnosis": "limited" if not gate_inventory_complete or "UNKNOWN_PARTIAL_CAUSE" in partial
        else "evidenced",
        "publication_metadata": fields, "entity_linkage_candidates": clusters, "scientific_gate_blockers": gate_blockers,
    }
    return record, causes


def summarize(records):
    results = Counter()
    for row in records:
        results.update({status: row[f"{status}_count"] for status in RESULT_STATES})
    counts = {name: sum(row[name] for row in records if row[name] is not None) for name in (
        "scientific_unresolved_count", "ocr_pending_pages_count", "pending_units", "layout_issue_count",
        "retry_parse", "checkpoint_error_count", "validation_error_count", "validation_warning_count",
        "planned_fragments", "total_fragment_records", "review_fragments",
    )}
    excluded = Counter()
    for row in records:
        excluded.update(row["excluded_result_rows"])
    checkpoints = Counter(r["checkpoint_status"] or r["checkpoint_currency"] for r in records)
    scope = Counter(r["scope_status"] for r in records)
    return {
        "registered": len(records),
        "checkpoint_statuses": {key: checkpoints[key] for key in ("processed", "partial", "running", "failed", "stale", "missing")},
        "results": {key: results[key] for key in RESULT_STATES},
        "scope": {key: scope[key] for key in ("in_scope", "out_of_scope", "needs_review")},
        "current_checkpoint_metrics": counts,
        "unknown_counts": {name: sum(row[name] is None for row in records) for name in counts},
        "partial_cause_documents": dict(Counter(c for r in records for c in r["partial_causes_json"])),
        "diagnosed_cause_documents": dict(Counter(c for r in records for c in r["diagnosed_causes"])),
        "processing_mode": dict(Counter(r["processing_mode"] or "UNKNOWN" for r in records)),
        "resume_observations": dict(Counter(r["resume_observation"] for r in records)),
        "publications_with_ocr": sum(bool(r["ocr_pending_pages_count"]) for r in records),
        "publications_with_needs_review": sum(bool(r["needs_review_count"]) for r in records),
        "excluded_result_rows": dict(excluded),
    }


def verify_report(records, causes, counts):
    keys = {row["document_id"] for row in records}
    if len(keys) != len(records):
        raise ValueError("Duplicate document diagnoses")
    ids = [result["result_id"] for row in records for result in row["results"]]
    if len(ids) != len(set(ids)) or sum(row["result_count"] for row in records) != len(ids):
        raise ValueError("Result counts or ownership do not reconcile")
    for row in records:
        if row["result_count"] != sum(row[f"{status}_count"] for status in RESULT_STATES):
            raise ValueError("Result statuses do not reconcile")
        fragments = row["total_fragment_records"]
        if fragments is not None and fragments != sum(row[f"{status}_fragments"] for status in FRAGMENT_STATES):
            raise ValueError("Fragment statuses do not reconcile")
        if row["scope_human_approved"] != (row["scope_status"] in {"in_scope", "out_of_scope"}):
            raise ValueError("Final and pending scope decisions do not reconcile")
        if row["checkpoint_currency"] != "current" and row["checkpoint_status"] is not None:
            raise ValueError("Stale checkpoints must have no current checkpoint status")
    if sum(sum(t["checkpoint_statuses"].values()) for t in counts.values()) != len(records):
        raise ValueError("Checkpoint statuses do not reconcile")
    if any(c["document_id"] not in keys or not c["explanation"] or not c["suggested_resolution"] for c in causes):
        raise ValueError("Cause lacks a publication, explanation or resolution")
