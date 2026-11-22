"""Corpora in these tests are synthetic. Real 180-PDF corpus audit is NOT RUN in CI."""

from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path

import pymupdf
import pytest

from spi import corpus_audit, corpus_audit_inputs
from spi.batch_evidence import PROFILES, SCHEMA_VERSIONS
from spi.batch_pdf import LAYOUT_VERSION
from spi.build_scope_sheet import FIELDS, QUEUE_FIELDS
from spi.storage import Store


def pdf_bytes():
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.set_metadata({"title": "Synthetic audit fixture", "author": "Example author"})
        return pdf.tobytes()


def result(doc, suffix="one", status="verified", **context):
    key = doc["document_id"]
    return {"result_id": f"{key}:{suffix}", "document_id": key, "domain": doc["domain"],
            "metric": {"name": "recall" if doc["domain"] == "rag" else "PCE",
                       "k": 5 if doc["domain"] == "rag" else None},
            "raw": {"value": "15", "unit": "%"}, "normalized": {"value": "0.15"},
            "source": {"page": 1, "table": "Table 1", "origin": "primary",
                       "fragment": "PRIVATE SYNTHETIC QUOTE"},
            "context": context, "extraction_status": status,
            "extraction_metadata": {"model_id": "local", "method": "pymupdf_verified_table",
                                    "pdf_sha256": doc["sha256"], "profile": "fixture", "schema_version": "1",
                                    "source_block_id": f"block-{suffix}"}}


def checkpoint(doc, status="partial", **overrides):
    key, digest, domain = (doc[k] for k in ("document_id", "sha256", "domain"))
    return {"document_id": key, "sha256": digest, "domain": domain, "status": status, "page_count": 2,
            "revision": f"{digest}|{LAYOUT_VERSION}|{PROFILES[domain]}|{SCHEMA_VERSIONS[domain]}|local",
            "mode": "local", "extraction_coverage": "partial_table_only", "scope_status_at_run": "needs_review",
            "planned_fragments": 2, "fragments": {}, "pending_units": 0, "pages_needing_ocr": [],
            "layout_issues": [], "errors": [], "retry_parse": False, **overrides}


def done(record):
    return {"status": "done", "page": 1, "results": [record]}


class Corpus:
    def __init__(self, path):
        self.root = path / "inputs"
        self.store = Store(self.root / corpus_audit_inputs.DEFAULTS["state"])
        self.content = pdf_bytes()
        self.docs, self.scopes, self.open = [], [], []

    def add(self, key="solar:P002", scope=None, **fields):
        track, pid = key.split(":")
        relative = f"data/{track}/test/{pid}.pdf"
        pdf = self.root / relative
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(self.content)
        digest = sha256(self.content).hexdigest()
        doc = {"document_id": key, "domain": track, "publication_id": pid, "sha256": digest,
               "file_path": str(pdf), "page_count": 2, **fields}
        self.docs.append(doc)
        if scope:
            self.scopes.append({"document_id": key, "pdf_sha256": digest, "status": scope,
                                "source_pages": [1], "reason": "Synthetic human decision"})
        else:
            self.open.append({
                "document_id": key, "domain": track, "publication_id": pid, "pdf_sha256": digest,
                "pdf_path": relative, "page_count": "2", "preliminary_status": "needs_review",
                "title": "Fixture worksheet candidate",
            })
        return doc

    def save(self):
        self.store.write("documents.json", self.docs)
        self.store.write("publication_scopes.json", self.scopes)
        for name, fields in (("queue", sorted(QUEUE_FIELDS)), ("approval", FIELDS)):
            path = self.root / corpus_audit_inputs.DEFAULTS[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf8") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows({k: row.get(k, "") for k in fields} for row in self.open)
        baseline = self.root / corpus_audit_inputs.DEFAULTS["baseline"]
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(self.scopes), encoding="utf8")

    def audit(self, destination, *, domain="solar", pid="P002", flags=()):
        args = ["--root", str(self.root), "--output", str(destination), "--domain", domain, *flags]
        if pid is not None:
            args.extend(("--publication-id", pid))
        code = corpus_audit.main(args)
        filename = "corpus_audit.json" if code == 0 else "diagnostic.json"
        return code, json.loads((destination / filename).read_text(encoding="utf8"))


@pytest.fixture
def corpus(tmp_path):
    return Corpus(tmp_path)


@pytest.mark.parametrize(("changes", "expected"), [
    ({"pages_needing_ocr": [2]}, {"OCR_REQUIRED"}),
    ({"pending_units": 1, "fragments": {"pending-1": {"status": "pending", "page": 2}}}, {"FRAGMENTS_PENDING"}),
    ({"review": True}, {"FRAGMENTS_NEED_REVIEW"}),
    ({"layout_issues": [{"page": 1, "reason": "table_layout_unresolved", "source_block_id": "anchor"}]},
     {"LAYOUT_ISSUES"}),
    ({"retry_parse": True, "pages": [{"page": 1, "error": "RuntimeError: PRIVATE SYNTHETIC QUOTE"}]},
     {"PDF_PARSE_RETRY"}),
    ({"fragments": {}}, {"NO_EXTRACTED_RESULTS"}),
    ({"pages_needing_ocr": [2], "pending_units": 3, "retry_parse": True, "fragments": {}},
     {"OCR_REQUIRED", "FRAGMENTS_PENDING", "PDF_PARSE_RETRY", "NO_EXTRACTED_RESULTS"}),
    ({}, {"UNKNOWN_PARTIAL_CAUSE"}),
])
def test_partial_conditions(corpus, tmp_path, changes, expected):
    doc = corpus.add()
    updated = dict(changes)
    review = updated.pop("review", False)
    fragments = {"f1": done(result(doc))}
    fragments.update(updated.pop("fragments", {}))
    if changes.get("fragments") == {}:
        fragments = {}
    if review:
        fragments["f1"].update(status="review", issues=["origin_unresolved"])
    corpus.store.save_batch_document(checkpoint(doc, **updated, fragments=fragments))
    corpus.save()
    output = tmp_path / "audit"
    code, report = corpus.audit(output, flags=("--verify-pdfs", "--compare-readiness"))
    assert code == 0
    row = report["documents"][0]
    assert row["scope_status"] == "needs_review"
    assert set(row["partial_causes_json"]) == expected
    assert "SCOPE_NOT_FINAL" in row["diagnosed_causes"]
    assert "MODEL_REQUEST_BUDGET" not in row["diagnosed_causes"]
    assert row["present_not_extractable_count"] is None
    assert report["readiness_comparison"]["status"] == "NOT RUN"
    assert "PRIVATE SYNTHETIC QUOTE" not in (output / "corpus_audit.json").read_text(encoding="utf8")
    for finding in report["causes"]:
        assert finding["explanation"] and finding["suggested_resolution"]
    if review:
        assert row["resume_observation"] == "same_mode_and_path_can_skip_even_if_partial"
        assert row["scientific_unresolved_count"] == 0


def test_complete_scoped_record_and_unmapped_metadata(corpus, tmp_path):
    doc = corpus.add(scope="in_scope", DOI="10.0000/audit-fixture")
    corpus.store.save_batch_document(checkpoint(
        doc, "processed", fragments={"f1": done(result(doc))}, scope_status_at_run="in_scope"
    ))
    corpus.save()
    output = tmp_path / "audit"
    code, report = corpus.audit(output, flags=("--verify-pdfs",))
    row = report["documents"][0]
    assert code == 0 and row["partial_causes_json"] == [] and row["verified_count"] == 1
    assert row["scientific_gate_blockers"] == []
    assert row["publication_metadata"]["DOI"]["export_mapping"] == "ignored_by_exporter"
    assert row["publication_metadata"]["title"]["candidate_source"] == "embedded_pdf_metadata"
    assert row["publication_metadata"]["year"]["confirmation"] == "unknown"
    assert report["scientific_release"]["gates"]["solar"]["release_ready"]
    assert report["scientific_release"]["status"] == "NOT RUN"


def test_stale_checkpoint_and_unbound_results_are_not_counted(corpus, tmp_path):
    doc = corpus.add(scope="in_scope")
    stale = result(doc, "stale")
    stale["extraction_metadata"]["pdf_sha256"] = "d" * 64
    unbound = result(doc, "unbound")
    del unbound["extraction_metadata"]
    corpus.store.write("results.json", [stale, unbound, result(doc, "direct")])
    corpus.store.save_batch_document(checkpoint(
        doc, sha256="d" * 64, fragments={"f1": done(stale)}, pages_needing_ocr=[2],
    ))
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    row = report["documents"][0]
    assert code == 0 and row["checkpoint_currency"] == "stale"
    assert row["ocr_pending_pages_count"] is None and row["checkpoint_status"] is None
    assert row["stale_checkpoint"]["status"] == "partial"
    assert row["excluded_result_rows"] == {"stale_sha": 1, "missing_sha": 1, "stale_checkpoint_rows": 1}
    assert row["verified_count"] == 1 and row["result_count"] == 1
    assert report["summaries"]["solar"]["checkpoint_statuses"]["stale"] == 1
    assert report["summaries"]["solar"]["checkpoint_statuses"]["partial"] == 0


def test_missing_checkpoint_and_results(corpus, tmp_path):
    corpus.add(scope="in_scope")
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    row = report["documents"][0]
    assert code == 0 and row["checkpoint_currency"] == "missing"
    assert row["result_count"] == 0 and row["scientific_unresolved_count"] is None
    assert "NO_VERIFIED_RESULTS" in row["scientific_gate_blockers"]
    assert "CHECKPOINT_MISSING" in row["diagnosed_causes"]


def test_missing_local_corpus(corpus, tmp_path):
    doc = corpus.add()
    corpus.save()
    Path(doc["file_path"]).unlink()
    output = tmp_path / "audit"
    code, report = corpus.audit(output)
    assert code == 2 and "missing test PDFs" in report["detail"]
    assert [p.name for p in output.iterdir()] == ["diagnostic.json"]


def test_error_reading_store(corpus, tmp_path):
    corpus.add()
    corpus.save()
    (corpus.store.root / "documents.json").write_bytes(b"bad json")
    code, report = corpus.audit(tmp_path / "audit")
    assert code == 1 and report["status"] == "FAIL" and report["phase"] == "corpus"


def test_store_changes_during_copy(corpus, tmp_path, monkeypatch):
    corpus.add()
    corpus.save()
    copyfile = corpus_audit_inputs.shutil.copyfile

    def replace_registry(source, destination):
        copied = copyfile(source, destination)
        if source == corpus.store.root / "documents.json":
            source.write_bytes(source.read_bytes() + b" ")
        return copied

    monkeypatch.setattr(corpus_audit_inputs.shutil, "copyfile", replace_registry)
    output = tmp_path / "audit"
    code, report = corpus.audit(output)
    assert code == 2 and "changed during snapshot" in report["detail"]
    assert [p.name for p in output.iterdir()] == ["diagnostic.json"]


def test_running_writer_blocks_without_modifying_lock_files(corpus, tmp_path):
    corpus.add()
    corpus.save()
    with corpus.store.batch_lock():
        path = corpus.store.root / "batch-lock.sqlite3"
        digest = sha256(path.read_bytes()).hexdigest()
        code, report = corpus.audit(tmp_path / "audit")
        assert code == 2 and "batch-lock" in report["detail"]
        assert sha256(path.read_bytes()).hexdigest() == digest


def test_out_of_scope_and_no_automatic_review(corpus, tmp_path):
    doc = corpus.add(scope="out_of_scope")
    review = result(doc, status="needs_review", unconfirmed_context=["origin"])
    corpus.store.save_batch_document(checkpoint(
        doc, fragments={"f1": {"page": 1, "status": "review", "results": [review]}}
    ))
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    row = report["documents"][0]
    assert code == 0 and row["scope_status"] == "out_of_scope" and row["needs_review_count"] == 1
    assert row["scientific_gate_blockers"] == [] and not report["scientific_release"]["gates"]["solar"]
    review_actions = [a for a in report["actions"] if a["action_type"] == "HUMAN_SCIENTIFIC_REVIEW"]
    assert review_actions and all(a["requires_human"] and a["blocked_by"] == ["OUT_OF_SCOPE"] for a in review_actions)


def test_in_scope_correct_review_state_and_preserved_issue(corpus, tmp_path):
    doc = corpus.add(scope="in_scope")
    record = result(doc, status="needs_review", unconfirmed_context=["origin"])
    fragment = {"status": "review", "page": 1, "results": [record], "issues": ["origin_unresolved"],
                "review_details": [{"code": "unknown_metric", "source_block_id": "r1", "page": 1},
                                   {"code": "conflicting_units", "source_block_id": "r1", "page": 1}]}
    warning = {"issue_id": "fixture-warning", "domain": "solar", "rule_id": "unresolved_source",
               "reason": "Review this source", "result_ids": [record["result_id"]]}
    corpus.store.save_batch_document(checkpoint(doc, fragments={"f1": fragment}, science_issues=[warning]))
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    row = report["documents"][0]
    assert code == 0 and row["verified_count"] == 0 and row["needs_review_count"] == 1
    assert set(row["partial_causes_json"]) == {"FRAGMENTS_NEED_REVIEW"}
    assert row["scientific_unresolved_count"] == 1 and row["unresolved_items"][0]["source_block_id"] == "r1"
    assert row["validation_warning_count"] == 1 and report["issues_preservation"]["linked_to_needs_review"] == 1
    assert report["issues_preservation"]["potentially_omitted"] == 1
    assert "NO_VERIFIED_RESULTS" in row["scientific_gate_blockers"]
    assert not any(a["requires_gigachat"] for a in report["actions"])


def test_rag_metric_k_and_top_k_remain_distinct(corpus, tmp_path):
    doc = corpus.add("rag:P001", scope="in_scope")
    r1 = result(doc, top_k=12, dataset_name="Synthetic", retrieval_method="dense", generator_model="example")
    r2 = result(doc, "two", top_k=24, dataset_name="Synthetic", retrieval_method="dense", generator_model="example")
    corpus.store.save_batch_document(checkpoint(doc, fragments={"f1": done(r1), "f2": done(r2)}))
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit", domain="rag", pid="P001")
    row = report["documents"][0]
    assert code == 0
    assert {(r["metric_k"], r["top_k"]) for r in row["results"]} == {(5, 12), (5, 24)}
    assert [c["entity"] for c in row["entity_linkage_candidates"]] == ["datasets"]


def test_solar_devices_separate_signatures_and_deterministic_outputs(corpus, tmp_path):
    doc = corpus.add(scope="in_scope")
    fragments = {key: done(result(doc, key, device_label=label))
                 for key, label in zip(("f1", "f2", "f3"), ("device a", "device a", "device b"))}
    corpus.store.save_batch_document(checkpoint(doc, fragments=fragments))
    corpus.save()
    paths = [tmp_path / "audit-one", tmp_path / "audit-two"]
    reports = []
    for path in paths:
        code, report = corpus.audit(path)
        assert code == 0
        clusters = report["documents"][0]["entity_linkage_candidates"]
        assert len(clusters) == 1 and clusters[0]["signature"]["device_label"] == "device a"
        assert len(clusters[0]["evidence"]) == 2 and clusters[0]["confidence"] == "medium"
        assert str(corpus.root) not in (path / "corpus_audit.json").read_text(encoding="utf8")
        del report["audit_metadata"]["utc"]
        reports.append(report)
    assert reports[0] == reports[1]
    for file in ("corpus_audit.csv", "checkpoint_causes.csv", "actions.csv", "summary.md", "approved_scope_report.md"):
        assert (paths[0] / file).read_bytes() == (paths[1] / file).read_bytes()


def test_only_saved_budget_marker_is_attributed(corpus, tmp_path):
    doc = corpus.add()
    corpus.store.save_batch_document(checkpoint(
        doc, pending_units=1, mode="gigachat",
        fragments={"f1": {"status": "pending", "page": 1, "error": "request_budget_exhausted"}},
    ))
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    assert code == 0
    assert "MODEL_REQUEST_BUDGET" in report["documents"][0]["partial_causes_json"]
    assert any(a["requires_gigachat"] for a in report["actions"])


@pytest.mark.parametrize("defect", ["duplicate_document", "result_owner", "invalid_ocr", "unbound_source", "changed_pdf"])
def test_invalid_invariants_fail_without_a_scientific_report(corpus, tmp_path, defect):
    doc = corpus.add()
    if defect == "duplicate_document":
        corpus.docs.append(doc)
    elif defect == "result_owner":
        other = corpus.add("solar:P007")
        corpus.store.save_batch_document(checkpoint(doc, fragments={"f1": done(result(other))}))
    elif defect == "invalid_ocr":
        corpus.store.save_batch_document(checkpoint(doc, pages_needing_ocr=[3]))
    elif defect == "unbound_source":
        fragment = {"status": "review", "page": 1, "review_details": [{"pdf_sha256": "d" * 64}]}
        corpus.store.save_batch_document(checkpoint(doc, fragments={"f1": fragment}))
    elif defect == "changed_pdf":
        pdf = Path(doc["file_path"])
        pdf.write_bytes(pdf.read_bytes() + b"extra")
    corpus.save()
    output = tmp_path / "audit"
    code, report = corpus.audit(output)
    assert code == 1 and report["status"] == "FAIL"
    assert [p.name for p in output.iterdir()] == ["diagnostic.json"]


def test_refuses_output_inside_store_or_official_release(corpus, capsys):
    corpus.add()
    corpus.save()
    for target in (corpus.store.root / "audit", corpus.root / "outputs/p0-22-release/audit"):
        assert corpus_audit.main(["--root", str(corpus.root), "--output", str(target)]) == 1
        assert not target.exists()
    assert "outside scientific data and release outputs" in capsys.readouterr().err


def test_full_test_selection_stays_separate_from_synthetic_fixture(corpus, tmp_path):
    corpus.add()
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit", pid=None)
    assert code == 2 and "89 PDFs" in report["detail"]


def test_full_synthetic_shape_and_historical_comparison(corpus, tmp_path):
    for track in ("solar", "rag"):
        for n in range(1, 91):
            corpus.add(f"{track}:P{n:03d}")
    corpus.save()
    previous = corpus.root / "readiness.json"
    previous.write_text(json.dumps({"tracks": {
        "solar": {"registered": 90, "scope": {"needs_review": 89}, "results": {"verified": 3}},
        "rag": {"registered": 90, "unresolved": "UNKNOWN"},
    }}), encoding="utf8")
    code, report = corpus.audit(tmp_path / "audit", domain="all", pid=None,
                                flags=("--compare-readiness", "readiness.json"))
    assert code == 0
    assert Counter(d["domain"] for d in report["documents"]) == {"solar": 90, "rag": 90}
    assert report["audit_metadata"]["physical_pdf_verification"] == "NOT RUN"
    assert report["readiness_comparison"]["solar"]["scope_pending"]["delta"] == 1
    assert report["readiness_comparison"]["solar"]["verified"]["delta"] == -3
    assert report["readiness_comparison"]["rag"]["unresolved"]["delta"] is None


def test_missing_scientific_evidence_cannot_signal_go(corpus, tmp_path):
    doc = corpus.add(scope="in_scope")
    legacy = checkpoint(doc, "processed", fragments={"f1": done(result(doc))}, scope_status_at_run="in_scope")
    legacy.pop("layout_issues")
    corpus.store.save_batch_document(legacy)
    corpus.save()
    code, report = corpus.audit(tmp_path / "audit")
    assert code == 0 and report["scientific_release"]["status"] == "BLOCKED"
    row = report["documents"][0]
    assert row["scientific_unresolved_count"] is None
    assert "SCIENTIFIC_EVIDENCE_UNKNOWN" in row["scientific_gate_blockers"]


def test_source_change_after_render_emits_only_diagnostic(corpus, tmp_path, monkeypatch):
    corpus.add()
    corpus.save()
    write_reports = corpus_audit.write_reports
    release_path = corpus.root / "outputs/p0-22-release/export-final.ps1"
    release_path.write_text("# fixture release", encoding="utf8")

    def concurrent_release_write(output, report):
        write_reports(output, report)
        release_path.write_text("# new release", encoding="utf8")

    monkeypatch.setattr(corpus_audit, "write_reports", concurrent_release_write)
    output = tmp_path / "audit"
    code, report = corpus.audit(output)
    assert code == 2 and "Release files changed" in report["detail"]
    assert [p.name for p in output.iterdir()] == ["diagnostic.json"]
