"""Build a read-only evidence and action plan for the Solar and RAG test corpus."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory, gettempdir, mkdtemp

from pydantic import ValidationError

from .corpus_audit_analysis import (
    APPROVED_IDS, diagnose_document, identity_index, issue_diagnostics, summarize, verify_report,
)
from .corpus_audit_inputs import (
    DEFAULTS, RELEASE, AuditBlocked, capture, check_unchanged, protection_inventory, read_decisions,
    select_pdfs, validated_registry, verify_pdfs,
)
from .corpus_audit_reports import actions_for, prioritize, write_json, write_reports
from .review_scope import TRACKS
from .storage import Store
from .store_export import release_quality


SCHEMA_VERSION = 1
BASELINE = {
    "solar": dict(registered=90, processed=0, partial=90, verified=12, needs_review=375, unresolved=439,
                  ocr_pending_pages=2, in_scope=2, scope_pending=88, approved_unresolved=17),
    "rag": dict(registered=90, processed=0, partial=90, verified=24, needs_review=1263, unresolved=4684,
                ocr_pending_pages=207, in_scope=2, scope_pending=88, approved_unresolved=111),
}
NOTES = [
    "Verified describes individual result evidence. Checkpoint completion describes execution of the saved plan.",
    "Pending units, review source blocks and parser layout anchors are counted; saved review results are a separate metric.",
    "Individual IDs of unrecorded planned or pending fragments cannot be reconstructed from a checkpoint.",
    "A budget stop is often recorded only in the run events. A missing budget marker leaves this attribution unknown.",
    "Result rows without a current SHA are excluded. Unsupported internal result statuses appear as JSON null and CSV UNKNOWN.",
    "Local-only runs schedule table units and can leave other planned work unevaluated.",
    "Scientific coverage and bibliographic candidates still require review of originals and any supplements.",
]


def compact_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def source_inventory(groups, originals, protection):
    everything = {**groups, "original_pdfs": originals, "release_protection": protection}
    return {group: {name: {"sha256": item.sha256, "bytes": item.size}
                    for name, item in sorted(files.items())} for group, files in everything.items()}


def readiness_totals(summary, gate):
    checkpoint = summary["checkpoint_statuses"]
    unknown = summary["unknown_counts"]
    metrics = summary["current_checkpoint_metrics"]
    scope = summary["scope"]
    return {
        "registered": summary["registered"], "processed": checkpoint.get("processed", 0),
        "partial": checkpoint.get("partial", 0), "verified": summary["results"]["verified"],
        "needs_review": summary["results"]["needs_review"], "in_scope": scope.get("in_scope", 0),
        "scope_pending": scope.get("needs_review", 0),
        "unresolved": None if unknown["scientific_unresolved_count"] else metrics["scientific_unresolved_count"],
        "ocr_pending_pages": None if unknown["ocr_pending_pages_count"] else metrics["ocr_pending_pages_count"],
        "approved_unresolved": gate["unresolved"] if gate else None,
    }


def compare_readiness(args, snapshot, summaries, gates):
    if args.compare_readiness is None:
        return None
    if args.publication_id:
        return {"status": "NOT RUN", "detail": "Historical totals cover full tracks"}
    before = BASELINE
    if args.compare_readiness is not True:
        before = {}
        previous = json.loads((snapshot / "review/comparison").read_bytes())
        for track in TRACKS:
            row = previous["tracks"][track]
            if not isinstance(row, dict):
                continue
            results, scope, gate = (row.get(k) for k in ("results", "scope", "quality_gate"))
            fields = {k: row.get(k) for k in ("registered", "processed", "partial", "unresolved", "ocr_pending_pages")}
            if isinstance(results, dict):
                fields.update({k: results.get(k, 0) for k in ("verified", "needs_review")})
            if isinstance(scope, dict):
                fields.update(in_scope=scope.get("in_scope", 0), scope_pending=scope.get("needs_review", 0))
            if isinstance(gate, dict):
                fields["approved_unresolved"] = gate.get("unresolved")
            before[track] = fields
    comparison = {}
    for track, totals in before.items():
        if not summaries[track]["registered"]:
            continue
        now = readiness_totals(summaries[track], gates[track])
        comparison[track] = {
            name: {"earlier": old if type(old) is int else None, "current": now[name],
                   "delta": now[name] - old if type(old) is int and now[name] is not None else None}
            for name, old in totals.items()
        }
    return comparison or {"status": "NOT RUN", "detail": "Earlier report has no track totals for this selection"}


def build_report(args, snapshot, docs, selected, pdf_metadata):
    store = Store(snapshot / "store")
    decisions, scopes, queue, sheet = read_decisions(args, snapshot, docs, store)
    checkpoints, by_doc, results, excluded = identity_index(store, docs)
    issues, issues_by_doc, preservation = issue_diagnostics(store, docs, results, decisions, checkpoints)

    diagnoses, causes = [], []
    track_rows = defaultdict(list)
    by_key = {}
    approved_causes = defaultdict(list)
    for key in sorted(selected):
        doc = docs[key]
        item, findings = diagnose_document(
            doc, checkpoints.get(key), by_doc[key], issues_by_doc[key], decisions.get(key),
            scopes.get((key, doc["sha256"])), queue.get(key, {}), sheet.get(key, {}), pdf_metadata.get(key, {}),
            excluded[key],
        )
        diagnoses.append(item)
        by_key[key] = item
        track_rows[doc["domain"]].append(item)
        causes.extend(findings)
        if key in APPROVED_IDS:
            approved_causes[key] = [c for c in findings if c["cause_code"] in {
                "PDF_PARSE_RETRY", "CHECKPOINT_ERRORS", "MODEL_CONFIGURATION_ERROR", "MODEL_REQUEST_BUDGET",
            }]
    counts = {track: summarize(track_rows[track]) for track in TRACKS}
    verify_report(diagnoses, causes, counts)

    approved = dict.fromkeys(APPROVED_IDS)
    for key in approved.keys() & by_key.keys():
        row = by_key[key]
        approved[key] = {**row, "approved_details": {
            "context_reasons": dict(Counter(f for r in row["context_review"] for f in r["unconfirmed_context"])),
            "validation_issues": issues_by_doc[key], "checkpoint_diagnostics": approved_causes[key],
        }}
    gates = {}
    for track in TRACKS:
        hashes = {r["document_id"]: r["pdf_sha256"] for r in track_rows[track] if r["scope_status"] == "in_scope"}
        # The release gate intentionally uses its existing counting and issue attribution rules.
        gates[track] = release_quality(hashes, list(results.values()), list(checkpoints.values()),
                                       [(owner, issue) for owner, issue in issues if issue.domain == track]) if hashes else None
    pending = sum(d["scope_status"] == "needs_review" for d in diagnoses)
    evidence_gaps = [d["document_id"] for d in diagnoses if d["scope_status"] == "in_scope"
                     and d["scientific_unresolved_count"] is None]
    tracks_without_in_scope = [t for t in TRACKS if track_rows[t] and gates[t] is None]
    full_selection = args.domain == "all" and args.publication_id is None
    blocked = (pending > 0 or bool(evidence_gaps) or any(gate and not gate["release_ready"] for gate in gates.values())
               or (full_selection and bool(tracks_without_in_scope)))
    actions = actions_for(diagnoses, causes)
    return {
        "status": "PASS", "schema_version": SCHEMA_VERSION,
        "audit_metadata": {"domain": args.domain, "publication_id": args.publication_id, "selected_pdf_count": len(selected),
                           "physical_pdf_verification": "PASS" if args.verify_pdfs else "NOT RUN",
                           "utc": datetime.now(UTC).isoformat()},
        "documents": diagnoses, "causes": causes, "actions": actions, "summaries": counts,
        "approved_scope_diagnostics": approved, "issues_preservation": preservation,
        "action_counts": dict(Counter(a["action_type"] for a in actions)), "top_publications": prioritize(diagnoses),
        "scientific_release": {
            "status": "BLOCKED" if blocked else "GO" if full_selection else "NOT RUN",
            "pending_scope": pending, "gates": gates, "publications_with_incomplete_gate_evidence": evidence_gaps,
            "tracks_without_in_scope": tracks_without_in_scope,
            "csv_format": "NOT RUN", "participant_validators": "NOT RUN",
        },
        "readiness_comparison": compare_readiness(args, snapshot, counts, gates),
        "technical_notes": NOTES,
        "quality_checks": {
            "selection_cardinality": "PASS", "unique_document_and_result_ownership": "PASS",
            "pdf_sha256_and_registry_identity": "PASS", "status_counts": "PASS", "ocr_range": "PASS",
            "scope_separation": "PASS", "cause_evidence_and_explanation": "PASS",
            "warnings_preserved": "PASS", "stale_checkpoints_excluded": "PASS", "unknown_values_marked": "PASS",
        },
    }


def run_audit(args, output):
    phase = "snapshot"
    try:
        with TemporaryDirectory(dir=output, prefix=".audit-") as work:
            work = Path(work)
            protection = protection_inventory(args.root)
            groups = capture(args, work)
            phase = "corpus"
            docs = validated_registry(Store(work / "store"))
            selected = select_pdfs(args, docs)
            originals, pdf_metadata = verify_pdfs(args, docs, selected)
            phase = "diagnosis"
            report = build_report(args, work, docs, selected, pdf_metadata)
            inventory = source_inventory(groups, originals, protection)
            verified_files = len(protection)
            report["source_inventory"] = inventory
            report["input_sha256"] = sha256(compact_json(inventory).encode("utf-8")).hexdigest()
            report["quality_checks"].update(snapshot_consistency="PASS", protected_inputs_sha256_unchanged="PASS")
            report["integrity"] = {
                "status": "PASS", "store_files": len(groups["store"]), "original_pdfs": len(originals),
                "review_files": len(groups["review"]), "protection_files": verified_files,
                "release_candidate_csv_files": sum(name.startswith("release_candidate_csv/") for name in protection),
                "pilot_official_format_csv_files": sum(name.startswith("pilot_official_format_csv/") for name in protection),
            }
            phase = "render"
            staged = work / "reports"
            staged.mkdir()
            write_reports(staged, report)
            phase = "source verification"
            check_unchanged(args, groups, originals, protection)
            try:
                stable_selection = select_pdfs(args, docs) == selected
            except (AuditBlocked, ValueError) as error:
                raise AuditBlocked("Original PDF inventory changed during the audit; retry with stable originals") from error
            if not stable_selection:
                raise AuditBlocked("Original PDF paths changed during the audit; retry with stable originals")
            phase = "report publication"
            for item in staged.iterdir():
                item.rename(output / item.name)
        return "PASS", output / "summary.md"
    except AuditBlocked as error:
        status, detail = "BLOCKED", str(error)
    except FileNotFoundError:
        status, detail = "BLOCKED", "A required file disappeared or is unavailable"
    except ValidationError as error:
        status, detail = "FAIL", f"Saved input violates its data model ({error.error_count()} validation errors)"
    except (csv.Error, json.JSONDecodeError, UnicodeError, KeyError, TypeError, AttributeError) as error:
        status, detail = "FAIL", f"Saved data or report has an invalid structure ({type(error).__name__})"
    except ValueError as error:
        status, detail = "FAIL", str(error)
    except OSError as error:
        status, detail = "FAIL", f"Could not read or write audit files ({type(error).__name__}, errno {error.errno})"
    path = output / "diagnostic.json"
    write_json(path, {"schema_version": SCHEMA_VERSION, "status": status, "phase": phase, "detail": detail})
    return status, path


def create_output(args):
    parent = args.output.parent if args.output else Path(gettempdir()).resolve()
    candidate = args.output or parent
    protected = [
        args.state, *(args.root / name for name in (
            "data", "state", ".git", RELEASE, "outputs/p0-21-test-local/d001-official", "outputs/contracts",
        )),
    ]
    if (any(candidate.is_relative_to(path.resolve()) for path in protected)
            or any((ancestor / "documents.json").is_file() or (ancestor / "publications.csv").is_file()
                   for ancestor in (candidate, *candidate.parents))):
        raise ValueError("Reports require a new directory outside scientific data and release outputs")
    if args.output is None:
        return Path(mkdtemp(prefix="spi-corpus-audit-", dir=parent))
    args.output.mkdir()
    return args.output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Workspace containing data/<domain>/test")
    for name, default in DEFAULTS.items():
        parser.add_argument(f"--{name}", type=Path, help=f"Source path, relative to --root by default ({default})")
    parser.add_argument("--output", type=Path, help="New report directory; defaults to a new system TEMP directory")
    parser.add_argument("--domain", choices=(*TRACKS, "all"), default="all")
    parser.add_argument("--publication-id", help="One PDF stem within the selected domain(s)")
    parser.add_argument("--compare-readiness", nargs="?", const=True, type=Path, metavar="REPORT",
                        help="Compare against the observed baseline or a previous release_readiness report")
    parser.add_argument("--verify-pdfs", action="store_true",
                        help="Also verify physical page counts and record embedded metadata candidates")
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    for name, default in DEFAULTS.items():
        setattr(args, name, (args.root / (getattr(args, name) or default)).resolve())
    if isinstance(args.compare_readiness, Path):
        args.compare_readiness = (args.root / args.compare_readiness).resolve()
    if args.output is not None:
        args.output = args.output.resolve()
    if args.publication_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.publication_id):
        parser.error("--publication-id must be a PDF stem")
    try:
        output = create_output(args)
    except (ValueError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    status, report = run_audit(args, output)
    print(f"{status}: {report}")
    return {"PASS": 0, "BLOCKED": 2, "FAIL": 1}[status]


if __name__ == "__main__":
    raise SystemExit(main())
