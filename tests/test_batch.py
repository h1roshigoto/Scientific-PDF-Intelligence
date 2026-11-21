import json
from hashlib import sha256
from time import time

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from spi import batch, cli
from spi.api import app
from spi.batch import discover, preview, report, run_batch
from spi.batch_checks import check_pce, validate_results
from spi.batch_evidence import offline_proposals, verify
from spi.batch_pdf import Cell, Fragment
from spi.csv_export import export_submission, read_submission
from spi.csv_schema import load_schema
from spi.gigachat import Completion, GigaChatClient, GigaChatSettings
from spi.gigachat_evidence import Block, EvidenceError
from spi.models import Evidence, Metric, PublicationScope, ResultRecord, Value
from spi.pdf import import_pdf
from spi.storage import Store
from spi.store_export import project_submission


def make_pdf(path, *, scan=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((45, 70), "Table 1. Our measurements under simulated AM 1.5G of 100 mW/cm2")
        for y, cells in [(90, ("Sample", "Voc (mV)", "PCE (%)")),
                         (107, ("A1", "1200", "15.5")), (124, ("B1", "1200", "15.6"))]:
            for x, value in zip((45, 135, 215), cells):
                # Centre all the numeric and header strings in their own columns.
                offset = pymupdf.get_text_length(value) / 2 if x > 50 else 0
                page.insert_text((x - offset, y), value)
        if scan:
            image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 100, 100), 0)
            image.clear_with(240)
            pdf.new_page().insert_image(pymupdf.Rect(10, 10, 300, 350), pixmap=image)
            pdf.new_page()
        pdf.save(path)
    return path


def test_batch_tracks_sha_ocr_idempotence_and_failure(tmp_path):
    solar = make_pdf(tmp_path / "solar" / "D001.pdf", scan=True)
    rag = make_pdf(tmp_path / "rag" / "D001.pdf")
    broken = solar.with_name("D002.pdf")
    broken.write_bytes(b"damaged")
    other = make_pdf(solar.with_name("D003.PDF"))
    store = Store(tmp_path / "state")
    previewed = preview(discover(solar.parent, limit=2), "solar", store)
    assert [d["status"] for d in previewed["documents"]] == ["pending", "pending"]
    assert store.documents() == []
    first = run_batch(store, discover(solar.parent), "solar", local_only=True)
    assert first["coverage"]["documents"] == {"partial": 1, "failed": 1, "processed": 1}
    assert first["documents"][0]["sha256"] == sha256(solar.read_bytes()).hexdigest()
    assert first["documents"][0]["pages_needing_ocr"] == [2]
    assert [p["read_status"] for p in first["documents"][0]["pages"]] == ["text", "ocr_required", "empty"]
    assert first["documents"][1]["errors"]
    assert first["documents"][2]["path"] == str(other)
    again = run_batch(store, discover(solar.parent), "solar", local_only=True)
    assert not again["requests_this_run"]
    assert first["coverage"] == again["coverage"]
    run_batch(store, [rag], "rag", local_only=True)
    assert {"solar:D001", "rag:D001", "solar:D003"} == {d["document_id"] for d in store.documents()}
    solar_results = [r for r in store.results("solar") if r.document_id == "solar:D001" and r.metric.name == "Voc"]
    assert {r.context["device_label"] for r in solar_results} == {"A1", "B1"}
    assert len({r.result_id for r in solar_results}) == 2


def test_rag_schema_update_reuses_legacy_solar_checkpoint(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "D001.pdf")
    store = Store(tmp_path / "state")
    with monkeypatch.context() as legacy:
        legacy.setitem(batch.SCHEMA_VERSIONS, "solar", "batch-evidence-7")
        first = run_batch(store, [path], "solar", local_only=True)

    assert first["requests_this_run"]
    resumed = run_batch(store, [path], "solar", local_only=True)
    assert not resumed["requests_this_run"]
    assert resumed["documents"] == first["documents"]


def test_interruption_resumes_fragments_and_uses_hash_cache(tmp_path):
    path = make_pdf(tmp_path / "D001.pdf")
    sibling = path.with_name("D999.pdf")
    sibling.write_bytes(path.read_bytes())
    store = Store(tmp_path / "state")
    calls = []

    def complete(prompt, *, schema):
        calls.append(prompt)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return Completion(model_id="GigaChat-3-Ultra", content=json.dumps({"results": []}))

    with pytest.raises(KeyboardInterrupt):
        run_batch(store, [path, sibling], "solar", local_only=False, complete=complete)
    saved = report(store, "solar")
    assert [d["status"] for d in saved["documents"]] == ["running", "pending"]
    first_doc = saved["documents"][0]
    assert len(first_doc["fragments"]) == 1

    resumed = run_batch(store, [path, sibling], "solar", local_only=False, complete=complete)
    assert all(d["status"] == "partial" for d in resumed["documents"])
    assert all(d["pending_units"] == 0 for d in resumed["documents"])
    assert calls.count(calls[0]) == 1
    assert any(event["cache_hit"] for event in resumed["requests_this_run"])


@pytest.mark.parametrize(("reason", "code"), [
    ("length", "finish_length"),
    ("blacklist", "finish_blacklist"),
    ("error", "finish_error"),
    (None, "http_error"),
])
def test_batch_cli_reports_failure_and_resumes_one_cell_at_a_time(tmp_path, monkeypatch, caplog, reason, code):
    pdf = make_pdf(tmp_path / "D001.pdf")
    state_dir = tmp_path / "state"
    report_path = tmp_path / "report.json"
    model = "GigaChat-3-Ultra:32.9.16.9"
    settings = GigaChatSettings(credentials=SecretStr("batchTestKey=="))
    sensitive = "batchTestKey== batch-test-oauth private-completion-body"
    requested_cells = []

    def handle(request):
        if request.url.path.endswith("/oauth"):
            return httpx.Response(200, json={"access_token": "batch-test-oauth", "expires_at": int(time()) + 1800})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["max_tokens"] == 1800
        assert body["response_format"]["schema"]["properties"]["results"]["maxItems"] == 1
        prompt, source = body["messages"][-1]["content"].split("\nPDF source:\n")
        assert "at most one scientific measurement" in prompt
        fragment = json.loads(source)
        cell, = fragment["table_cells"]
        requested_cells.append(cell["cell"])
        if len(requested_cells) == 1 and reason is None:
            return httpx.Response(503, json={"message": sensitive})
        blocks = {b["block_id"]: b["text"] for b in fragment["blocks"]}
        header = blocks[cell["headers"][-1]]
        metric, unit = header.rstrip(")").split(" (")
        value = blocks[cell["cell"]]
        prediction = {"results": [{
            "cell": cell["cell"], "row": cell["row"], "headers": cell["headers"],
            "block": cell["cell"], "quote": value, "raw_value": value, "raw_unit": unit,
            "metric": metric, "origin": "primary", "facts": {},
        }]}
        first = len(requested_cells) == 1
        return httpx.Response(200, json={
            "model": model,
            "choices": [{
                "message": {"content": json.dumps(prediction)},
                "finish_reason": reason if first else "stop",
            }],
            "usage": {"completion_tokens": 1800 if first else 125},
            "raw_private_diagnostic": sensitive,
        })

    monkeypatch.setattr(batch, "GigaChatClient", lambda **kwargs: GigaChatClient(
        settings, transport=httpx.MockTransport(handle), **kwargs,
    ))
    caplog.set_level("INFO", logger="spi.gigachat")

    def invoke(budget):
        assert cli.main([
            str(pdf), "--domain", "solar", "--batch", "--state", str(state_dir),
            "--report", str(report_path), "--max-requests", str(budget), "--completion-max-tokens", "1800",
        ]) == 0
        return json.loads(report_path.read_text(encoding="utf-8"))

    failed = invoke(1)
    assert len(requested_cells) == 1
    attempted = failed["requests_this_run"][0]
    assert attempted["error"] == code
    assert attempted["finish_reason"] == reason
    assert attempted["model_id"] == (model if reason else None)
    assert attempted["completion_tokens"] == (1800 if reason else None)
    assert attempted["requested_max_tokens"] == 1800
    assert attempted["retries"] == 0
    assert attempted["elapsed_ms"] >= 0
    assert attempted["http_status"] == (None if reason else 503)
    assert not failed["coverage"]["results"]
    document = failed["documents"][0]
    assert document["pending_units"] == document["planned_fragments"] >= 4
    assert document["fragments"][attempted["fragment"]]["completion"] == attempted
    for secret in sensitive.split():
        assert secret not in report_path.read_text(encoding="utf-8") + caplog.text

    budget_only = invoke(0)
    assert len(requested_cells) == 1
    assert budget_only["documents"][0]["fragments"][attempted["fragment"]]["completion"] == attempted

    resumed = invoke(1)
    assert len(requested_cells) == 2
    assert requested_cells[0] == requested_cells[1]
    assert resumed["coverage"]["results"] == {"verified": 1}
    assert resumed["requests_this_run"][0]["finish_reason"] == "stop"
    assert resumed["requests_this_run"][0]["completion_tokens"] == 125
    assert resumed["documents"][0]["pending_units"] == document["pending_units"] - 1

    next_cell = invoke(1)
    assert len(requested_cells) == 3
    assert requested_cells[2] != requested_cells[1]
    assert next_cell["coverage"]["results"] == {"verified": 2}
    assert next_cell["documents"][0]["pending_units"] == document["pending_units"] - 2


@pytest.mark.parametrize("limit", ["0", "-1", "4097", "1800.5"])
def test_batch_cli_rejects_invalid_completion_limit(tmp_path, capsys, limit):
    with pytest.raises(SystemExit) as caught:
        cli.main([str(tmp_path), "--domain", "solar", "--batch", "--completion-max-tokens", limit])
    assert caught.value.code == 2
    assert "--completion-max-tokens" in capsys.readouterr().err


def test_model_plan_keeps_prior_grouped_results_and_schedules_uncovered_cells(tmp_path):
    pdf = make_pdf(tmp_path / "D001.pdf")
    store = Store(tmp_path / "state")
    local = run_batch(store, [pdf], "solar", local_only=True)["documents"][0]
    fragment_id, fragment = next(iter(local["fragments"].items()))
    assert len(fragment["results"]) == 2
    retained = fragment["results"][0]
    retained["extraction_metadata"]["model_id"] = "GigaChat-3-Ultra"
    local["fragments"] = {fragment_id: dict(fragment, results=[retained])}
    local.update(revision=local["revision"].removesuffix("|local") + "|gigachat", mode="gigachat")
    store.save_batch_document(local)

    resumed = run_batch(store, [pdf], "solar", local_only=False, max_requests=0)
    checkpoint = resumed["documents"][0]
    assert resumed["coverage"]["results"] == {"verified": 1}
    assert [row.result_id for row in store.results("solar")] == [retained["result_id"]]
    assert fragment_id not in checkpoint["fragments"]
    assert checkpoint["pending_units"] == checkpoint["planned_fragments"] - 1
    assert checkpoint["pending_units"] >= 3


def test_scope_explicit_and_versioned(tmp_path):
    path = make_pdf(tmp_path / "D001.pdf")
    store = Store(tmp_path / "state")
    initial = run_batch(store, [path], "solar", local_only=True)
    assert initial["scope_review_queue"] == ["solar:D001"]
    digest = initial["documents"][0]["sha256"]
    for status in ["out_of_scope", "in_scope", "needs_review"]:
        decision = PublicationScope(document_id="solar:D001", pdf_sha256=digest,
                                    status=status, reason="Document reviewed", source_pages=[1])
        store.save_publication_scopes([decision])
        current = run_batch(store, [path], "solar", local_only=True)
        assert current["documents"][0]["scope"]["status"] == status
        assert bool(current["scope_review_queue"]) == (status == "needs_review")
    with pytest.raises(ValueError, match="does not match"):
        store.save_publication_scopes([decision.model_copy(update={"pdf_sha256": "0"*64})])


def test_d021_discrepancy_exports_both_sources_and_follows_scope(tmp_path, monkeypatch):
    path = tmp_path / "D021.pdf"
    document_id = "solar:D021"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((45, 60), "Table 4. B4 (3.85)")
        pdf.new_page().insert_text((45, 60), "Figure 10. B4 PCE = 5.25%")
        pdf.save(path)
    store = Store(tmp_path / "state")
    doc = import_pdf(store, path, "solar")
    originals = [
        ("table", "3.85", Evidence(page=1, table="Table 4", fragment="(3.85)", origin="primary")),
        ("figure", "5.25", Evidence(page=2, figure="Figure 10", fragment="5.25%", origin="primary")),
    ]
    results = [
        ResultRecord(result_id=f"{document_id}:{key}", document_id=document_id, domain="solar",
                     metric=Metric(name="PCE"), raw=Value(value=value, unit="%"),
                     normalized=Value(value=value, unit="%"), source=source,
                     context={"device_label": "B4", "aggregation": "champion",
                              "measurement_type": "J-V" if key == "figure" else None},
                     extraction_status="verified",
                     extraction_metadata={"pdf_sha256": doc["sha256"], "model_id": "local",
                                          "method": "pymupdf_verified_table",
                                          "profile": "solar", "schema_version": "test"})
        for key, value, source in originals
    ]
    _, (issue,) = validate_results(results)
    assert issue.rule_id == "table_figure_difference"
    # A linked result under review cannot become a foreign key in the submission.
    unverified = results[0].model_copy(update={"result_id": f"{document_id}:review",
                                               "extraction_status": "needs_review"})
    issue.result_ids.append(unverified.result_id)
    checkpoint = {**doc, "fragments": {"b4": {"results": [r.model_dump() for r in [*results, unverified]]}},
                  "science_issues": [issue.model_dump()]}
    store.save_batch_document(checkpoint)
    monkeypatch.setenv("SPI_STATE_DIR", str(store.root))
    client = TestClient(app)
    schema = load_schema("solar")

    def set_scope(version, status):
        store.save_publication_scopes([
            PublicationScope(document_id=document_id, pdf_sha256=version["sha256"], status=status,
                             reason="Reviewed publication scope", source_pages=[1]),
        ])

    def export(name):
        submission = project_submission(store, schema, tmp_path, publication_ids=["D021"])
        destination = export_submission(schema, submission, tmp_path / name, publication_ids=["D021"])
        return read_submission(schema, destination).tables

    set_scope(doc, "in_scope")
    rows = export("included")
    assert [row["raw_value"] for row in rows["results"]] == ["3.85", "5.25"]
    assert client.get("/issues?domain=solar").json() == [issue.model_dump()]
    exported_issue, = rows["issues"]
    assert tuple(exported_issue) == ("publication_id", "issue_id", "record_ids", "check_name",
                                     "description", "source_page", "source_fragment")
    assert exported_issue["publication_id"] == "D021"
    assert exported_issue["issue_id"] == issue.issue_id
    assert exported_issue["check_name"] == issue.rule_id
    assert exported_issue["record_ids"].split(";") == [r.result_id for r in results]
    assert exported_issue["source_page"] == "1"
    assert exported_issue["source_fragment"] == "(3.85)"
    assert json.loads(exported_issue["description"].split("Evidence references (JSON)\n")[1]) == issue.evidence_refs

    set_scope(doc, "out_of_scope")
    rows = export("excluded")
    assert rows["publications"][0]["in_scope"] == "false"
    assert all(not records for table, records in rows.items() if table != "publications")
    assert client.get("/documents?domain=solar").json()[0]["document_id"] == document_id
    assert client.get("/results").json() == client.get("/issues").json() == []
    assert client.get(f"/results/{results[0].result_id}").status_code == 404
    assert store.results() == [*results, unverified]
    assert store.issues() == [issue]
    assert store.batch_documents("solar") == [checkpoint]

    set_scope(doc, "in_scope")
    assert client.get(f"/results/{results[0].result_id}").json()["raw"]["value"] == "3.85"
    assert client.get("/issues").json() == [issue.model_dump()]
    assert export("restored")["issues"] == [exported_issue]

    replacement = make_pdf(tmp_path / "revised.pdf")
    replacement.replace(path)
    revised = import_pdf(store, path, "solar")
    assert revised["sha256"] != doc["sha256"]
    set_scope(revised, "in_scope")
    assert client.get("/results").json() == client.get("/issues").json() == []
    rows = export("revised")
    assert rows["results"] == rows["issues"] == []
    assert store.batch_documents("solar") == [checkpoint]


def test_verifier_requires_exact_quote_row_column_and_known_unit():
    def b(key, text):
        return Block(key, (0, 0, 1, 1), text)

    caption = b("caption", "Table 1: Our measurements")
    first = Cell(b("a", "1200"), (b("row1", "A1"),), (b("col1", "Voc (mV)"),))
    second = Cell(b("b", "1200"), (b("row2", "B1"),), (b("col2", "Voc (mystery)"),))
    blocks = (caption, first.value, *first.row, *first.headers, second.value, *second.row, *second.headers)
    fragment = Fragment("table", 1, blocks, (first, second), caption, "Table 1")
    p1, p2 = offline_proposals(fragment, "solar")

    def check(proposal):
        return verify(proposal, fragment, "solar", "solar:D001", "a"*64, "local")[0]

    assert check(p1).normalized.value == "1.200"
    assert check(p2).extraction_status == "needs_review"
    assert check(p2).normalized.value is None
    with pytest.raises(EvidenceError, match="quote_mismatch"):
        check(p1.model_copy(update={"quote": "1199"}))
    with pytest.raises(EvidenceError, match="wrong_table_cell"):
        check(p1.model_copy(update={"block": "b", "cell": "b"}))
    with pytest.raises(EvidenceError, match="wrong_table_cell"):
        check(p1.model_copy(update={"headers": ["col2"]}))


def test_rag_metric_k_is_distinct_from_retrieval_top_k():
    def block(key, value):
        return Block(key, (0, 0, 1, 1), value)

    caption = block("caption", "Table 3. Our evaluation\nDataset: Benchmark\nGenerator: Model\nRetrieval top-k: 20")
    row = block("row", "Method")
    header = block("col", "Recall@5 (%)")
    value = block("cell", "72")
    cell = Cell(value, (row,), (header,))
    fragment = Fragment("table", 2, (caption, row, header, value), (cell,), caption, "Table 3")
    (proposal,) = offline_proposals(fragment, "rag")
    result, errors = verify(proposal, fragment, "rag", "rag:D101", "b"*64, "local")
    assert not errors
    assert result.metric.k == 5
    assert result.context["top_k"] == "20"
    assert result.normalized.unit == "%"


@pytest.mark.parametrize(("pce", "pin", "comparable", "status"), [
    ("18.4", "100", True, "pass"), ("19.5", "100", True, "fail"),
    ("18.4", None, True, "not_evaluable"), ("18.4", "100", False, "not_evaluable"),
])
def test_pce(pce, pin, comparable, status):
    check = check_pce({"PCE": pce, "Voc": 1.15, "Jsc": 20, "FF": .80}, pin, comparable=comparable)
    assert check["status"] == status
    if status == "fail":
        assert check["published_pce_percent"] == pce
        assert float(check["calculated_pce_percent"]) == 18.4
