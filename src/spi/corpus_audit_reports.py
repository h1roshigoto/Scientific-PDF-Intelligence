"""Render the corpus audit from the validated report data."""

from collections import Counter, defaultdict
import csv
import json


DOCUMENT_FIELDS = (
    "document_id domain publication_id pdf_sha256 pdf_pages scope_status scope_decision_source "
    "scope_human_approved scope_pending_reason checkpoint_status checkpoint_revision processing_mode "
    "extraction_coverage scope_status_at_run planned_fragments total_fragment_records pending_fragments "
    "review_fragments done_fragments failed_fragments pending_units retry_parse layout_issue_count "
    "ocr_pending_pages_count ocr_pending_pages_json checkpoint_error_count result_count verified_count "
    "needs_review_count extracted_count absent_count present_not_extractable_count validation_error_count "
    "validation_warning_count scientific_unresolved_count partial_causes_json primary_blocker recommended_action "
    "confidence_of_diagnosis checkpoint_currency resume_observation"
).split()
CAUSE_FIELDS = (
    "document_id pdf_sha256 checkpoint_revision cause_code severity fragment_id source_block_id source_page "
    "evidence_field evidence_value explanation suggested_resolution category partial_condition result_id action_type"
).split()
ACTION_FIELDS = (
    "action_id document_id cause_code action_type action_priority requires_human requires_ocr requires_gigachat "
    "requires_code_change suggested_next_step source_evidence blocked_by"
).split()


def actions_for(records, causes):
    grouped = defaultdict(list)
    for row in causes:
        grouped[(row["document_id"], row["cause_code"], row["action_type"])].append(row)
    docs = {row["document_id"]: row for row in records}
    actions = []
    for (key, cause, action), evidence in sorted(grouped.items()):
        row = docs[key]
        category = evidence[0]["category"]
        blocked_by = []
        if row["scope_status"] == "out_of_scope" and category != "submission":
            blocked_by.append("OUT_OF_SCOPE")
        elif row["scope_status"] == "needs_review" and category != "scope":
            blocked_by.append("SCOPE_NOT_FINAL")
        if cause in {"FRAGMENTS_PENDING", "FRAGMENTS_NEED_REVIEW", "RESULTS_NEED_REVIEW",
                     "SCIENTIFIC_CONTEXT_INCOMPLETE", "NO_VERIFIED_RESULTS", "NO_EXTRACTED_RESULTS"}:
            blocked_by.extend(c for c in ("PDF_PARSE_RETRY", "OCR_REQUIRED") if c in row["diagnosed_causes"])
        priority = ("P3" if blocked_by or category == "submission" else
                    "P0" if row["scope_status"] == "in_scope" else "P1" if category == "scope" else "P2")
        if category == "science":
            human, code = True, False
        elif category == "submission":
            human, code = True, action in {"FIX_EXPORT_MAPPING", "FIX_ENTITY_LINKAGE"}
        else:
            human = action in {"HUMAN_SCOPE_REVIEW", "INVESTIGATE_UNKNOWN"}
            code = action == "FIX_EXTRACTION_LOGIC"
        actions.append({
            "action_id": f"{key}:{cause}:{action}", "document_id": key, "cause_code": cause,
            "action_type": action, "action_priority": priority,
            "requires_human": human, "requires_ocr": action == "OCR_REQUIRED",
            "requires_gigachat": action == "RESUME_MODEL_EXTRACTION" and row["processing_mode"] == "gigachat",
            "requires_code_change": code, "suggested_next_step": evidence[0]["suggested_resolution"],
            "source_evidence": {
                "table": "checkpoint_causes.csv", "count": len(evidence),
                "where": {"document_id": key, "cause_code": cause, "action_type": action},
                "examples": [{field: c[field] for field in (
                    "evidence_field", "evidence_value", "fragment_id", "source_block_id", "source_page", "result_id",
                )} for c in evidence[:5]],
            },
            "blocked_by": blocked_by,
        })
    return sorted(actions, key=lambda row: (row["action_priority"], row["action_id"]))


def prioritize(records):
    def priority(row):
        return (0 if row["scope_status"] == "in_scope" else 1 if row["scope_status"] == "needs_review" else 2,
                -int(bool(row["retry_parse"])), -(row["ocr_pending_pages_count"] or 0),
                -int("NO_VERIFIED_RESULTS" in row["scientific_gate_blockers"]),
                -(row["scientific_unresolved_count"] or 0), -row["needs_review_count"], row["document_id"])

    return [{"document_id": d["document_id"], "primary_blocker": d["primary_blocker"],
             "scope_status": d["scope_status"], "ocr_pages": d["ocr_pending_pages_count"],
             "parse_retry": d["retry_parse"], "unresolved": d["scientific_unresolved_count"],
             "needs_review": d["needs_review_count"], "next_step": d["recommended_action"]}
            for d in sorted((r for r in records if r["primary_blocker"]), key=priority)[:20]]


def csv_cell(value, *, unknown=True):
    if value is None:
        return "UNKNOWN" if unknown else ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, fields, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_cell(row[field], unknown=field != "fragment_id") for field in fields})


def write_json(path, report):
    with path.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, indent=2, ensure_ascii=False, sort_keys=True)
        file.write("\n")


def show(value):
    return "UNKNOWN" if value is None else str(value)


def markdown_table(headers, rows):
    def line(values):
        return "| " + " | ".join(show(v).replace("|", r"\|").replace("\n", " ") for v in values) + " |"

    return [line(headers), line(["---"] * len(headers)), *(line(r) for r in rows)]


def markdown_summary(report):
    docs = report["documents"]
    counts = report["summaries"]
    issue_summary = report["issues_preservation"]
    categories = Counter(c for d in docs for c in d["partial_causes_json"])
    metadata = Counter(f"{name} {value['store_status']}" for d in docs for name, value in d["publication_metadata"].items())
    cluster_counts = Counter(c["entity"] for d in docs for c in d["entity_linkage_candidates"])
    lines = ["# Scientific corpus audit", "", f"Audit **{report['status']}** · Scientific release "
             f"**{report['scientific_release']['status']}**", "",
             "## 1. Corpus", "",
             f"Original PDF hashes checked **{len(docs)}**. Physical page counts "
             f"**{report['audit_metadata']['physical_pdf_verification']}**.", ""]
    for index, track in enumerate(("solar", "rag"), 2):
        t = counts[track]
        lines.extend([f"## {index}. {track.upper()}", ""])
        if not t["registered"]:
            lines += ["Track outside the selection.", ""]
            continue
        lines += markdown_table(("Metric", "Count"), [
            ("Registered", t["registered"]), *t["checkpoint_statuses"].items(), *t["results"].items(),
            *[(f"Scope {state}", value) for state, value in t["scope"].items()],
            *[(name, f"{number} known ({t['unknown_counts'][name]} unknown)"
               if t["unknown_counts"][name] else number)
              for name, number in t["current_checkpoint_metrics"].items()],
        ]) + [""]
    lines += ["## 4. Partial causes", ""]
    lines += markdown_table(("Cause", "Publications"), sorted(categories.items())) + [
        "", "A publication can occur in several rows. Scope, submission and scientific gate findings are listed "
        "separately from the batch partial conditions.", "",
    ]
    for title, metric, field in (
        ("5. Scope review backlog", "needs_review", "scope"),
        ("6. OCR backlog", "ocr_pending_pages_count", "current_checkpoint_metrics"),
        ("7. Scientific review backlog", "needs_review", "results"),
        ("8. Model and processing backlog", "pending_units", "current_checkpoint_metrics"),
        ("9. Parser and layout backlog", "layout_issue_count", "current_checkpoint_metrics"),
    ):
        lines += [f"## {title}", ""]
        lines += markdown_table(("Track", "Known count", "Unknown publications"), [
            (t.upper(), c[field].get(metric, 0), c["unknown_counts"].get(metric, 0))
            for t, c in counts.items() if c["registered"]
        ]) + [""]
        if metric == "pending_units":
            lines += ["Pending units are classified using the saved mode. Local omissions outside the scheduled "
                      "table units are not model requests.", ""]
            lines += markdown_table(("Track", "Processing mode", "Checkpoints"), [
                (track.upper(), mode, count) for track, c in counts.items()
                for mode, count in sorted(c["processing_mode"].items())
            ]) + [""]
        if metric == "layout_issue_count":
            lines += [f"Parser retry flags recorded for {sum(r['retry_parse'] is True for r in docs)} publications.", ""]
    lines += ["## 10. Errors and warnings", "",
              f"Current Store issue severities {json.dumps(issue_summary['by_severity'], sort_keys=True)}. "
              f"Checkpoint errors {sum(r['checkpoint_error_count'] or 0 for r in docs)} known.", "",
              "## 11. Previously confirmed publications", "",
              "See [approved_scope_report.md](approved_scope_report.md) for the identities, "
              "fragment and page inventory, and evidence limits.", "",
              "## 12. Publication metadata", ""]
    lines += markdown_table(("Field availability in Store", "Publications"), sorted(metadata.items())) + [
        "", "Worksheet and PDF metadata are candidates requiring verification. "
        "The current exporter populates the publication ID, inclusion and exclusion reason.", "",
        "## 13. Entity linkage candidates", "",
    ]
    lines += markdown_table(("Entity", "Candidate clusters"), sorted(cluster_counts.items())) + [
        "", "Review each cluster against its source context before sharing IDs.", "",
        "## 14. Issues preservation", "",
        f"Store issues {issue_summary['total_in_store']}. Current {issue_summary['current']}. "
        f"Linked to verified {issue_summary['linked_to_verified']}. "
        f"Linked to needs_review {issue_summary['linked_to_needs_review']}. "
        f"Potentially omitted {issue_summary['potentially_omitted']}.", "",
        "Solar's pinned contract excludes issues.csv. The RAG mapping requires a verified current result from an "
        "included publication. See JSON for the stored issue references and omission rationale.", "",
        "## 15. Data limits", "",
        *(f"- {text}" for text in report["technical_notes"]), "",
        "## 16. Repair order", "",
        "Confirm scope, prioritizing already included publications for technical and scientific work. "
        "Resolve parser and OCR blockers before adjudicating measurements. Then address export mapping and linkage.", "",
        "Ranked by confirmed inclusion, parser retry, OCR pages, included publications with zero verified results, unresolved count and review results, in that order.", "",
    ]
    lines += markdown_table(("Publication", "Primary blocker", "Scope", "Parser retry", "OCR", "Unresolved", "Review"),
                            [(r["document_id"], r["primary_blocker"], r["scope_status"], r["parse_retry"], r["ocr_pages"],
                              r["unresolved"], r["needs_review"]) for r in report["top_publications"]]) + [""]
    lines += markdown_table(("Action type", "Tasks"), sorted(report["action_counts"].items())) + [
        "", "## 17. Scientific release blockers", "",
        f"Scope pending **{report['scientific_release']['pending_scope']}**. "
        f"CSV format and participant validators **NOT RUN**.", "",
    ]
    for track, gate in report["scientific_release"]["gates"].items():
        if track in report["scientific_release"]["tracks_without_in_scope"]:
            lines.append(f"- {track.upper()} has no selected, confirmed in-scope publication.")
        if gate:
            lines.append(f"- {track.upper()} approved in-scope gate **{gate['status']}**. "
                         f"{len(gate['incomplete_checkpoints'])} incomplete checkpoints, "
                         f"{len(gate['publications_without_verified_results'])} publications without verified results, "
                         f"{gate['needs_review']} review records, {gate['unresolved']} known unresolved units, "
                         f"{len(gate['error_issues'])} error issues.")
    gaps = report["scientific_release"]["publications_with_incomplete_gate_evidence"]
    if gaps:
        lines.append(f"- Checkpoint evidence insufficient for {len(gaps)} included publications.")
    comparison = report["readiness_comparison"]
    if comparison and "status" in comparison:
        lines += ["", f"Readiness comparison **{comparison['status']}**. {comparison['detail']}"]
    elif comparison:
        lines += ["", "### Readiness comparison", "",
                  "Current counts require PDF-bound results and complete current checkpoint data. "
                  "The earlier report provides aggregate totals.", ""]
        lines += markdown_table(("Track", "Metric", "Earlier", "Current", "Delta"), [
            (track, metric, row["earlier"], row["current"], row["delta"])
            for track, metrics in comparison.items() for metric, row in metrics.items()
        ])
    return "\n".join(lines) + "\n"


def approved_markdown(report):
    lines = ["# Previously confirmed scope publications", "",
             "Counts combine saved layout anchors, distinct source blocks per review fragment, "
             "review fragments without results or details, and pending units. Review records are counted separately.", ""]
    for key, row in report["approved_scope_diagnostics"].items():
        lines += [f"## {key}", ""]
        if row is None:
            lines += ["Publication outside this selection.", ""]
            continue
        lines += [
            f"- PDF SHA-256 `{row['pdf_sha256']}`. Scope {row['scope_status']}, "
            f"human-approved {row['scope_human_approved']}, source {row['scope_decision_source']}, "
            f"evidence pages {row['scope_evidence_pages']}.",
            f"- Checkpoint {show(row['checkpoint_status'])} ({row['checkpoint_currency']}), "
            f"scope at run {show(row['scope_status_at_run'])}.",
            f"- Parser retry {show(row['retry_parse'])}, checkpoint errors {show(row['checkpoint_error_count'])}, "
            f"OCR pages {show(row['ocr_pending_pages_json'])}, layout issues {show(row['layout_issue_count'])}.",
            f"- Planned fragments {show(row['planned_fragments'])}, pending fragment records "
            f"{show(row['pending_fragments'])}, pending units {show(row['pending_units'])}, "
            f"review fragments {show(row['review_fragments'])}.",
            f"- Verified {row['verified_count']}, needs_review {row['needs_review_count']}, "
            f"validation errors {row['validation_error_count']}, warnings {row['validation_warning_count']}.",
            f"- Gate blockers {', '.join(row['scientific_gate_blockers']) or 'None recorded'}.", "",
        ]
        details = row["approved_details"]
        if details["context_reasons"]:
            lines += ["Unconfirmed context by result", ""]
            lines += markdown_table(("Field", "Results"), sorted(details["context_reasons"].items())) + [""]
        if row["document_id"] == "solar:P006" and row["verified_count"] == 0:
            lines += ["The saved checkpoint yields zero verified results. The review records and their unconfirmed "
                      "fields establish which evidence is still needed. A separate scientific review must determine "
                      "whether other eligible primary measurements exist.", ""]
        if details["checkpoint_diagnostics"]:
            lines += markdown_table(("Cause", "Page", "Field", "Value"), [
                (c["cause_code"], c["source_page"], c["evidence_field"], c["evidence_value"])
                for c in details["checkpoint_diagnostics"]
            ]) + [""]
        if details["validation_issues"]:
            lines += markdown_table(("Issue", "Rule", "Severity"), [
                (i["issue_id"], i["rule_id"], i["severity"]) for i in details["validation_issues"]
            ]) + [""]
        lines += [f"**Known scientific unresolved items {show(row['scientific_unresolved_count'])}.**", ""]
        lines += markdown_table(("Kind", "Count", "Fragment ID", "Source block", "Page", "Reason"), [
            (v["kind"], v["count"], v["fragment_id"], v["source_block_id"], v["source_page"], v.get("reason"))
            for v in row["unresolved_items"] or []
        ]) + [""]
    return "\n".join(lines) + "\n"


def write_reports(output, report):
    for filename, fields, rows in (
        ("corpus_audit.csv", DOCUMENT_FIELDS, report["documents"]),
        ("checkpoint_causes.csv", CAUSE_FIELDS, report["causes"]),
        ("actions.csv", ACTION_FIELDS, report["actions"]),
    ):
        write_csv(output / filename, fields, rows)
    write_json(output / "corpus_audit.json", report)
    (output / "summary.md").write_text(markdown_summary(report), encoding="utf-8")
    (output / "approved_scope_report.md").write_text(approved_markdown(report), encoding="utf-8")
