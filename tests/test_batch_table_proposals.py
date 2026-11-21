import json
from time import time

import httpx
import pymupdf
import pytest
from pydantic import SecretStr

from spi import batch, cli
from spi.batch_evidence import Proposal, normalize, verify
from spi.batch_pdf import Cell, Fragment
from spi.gigachat import GigaChatClient, GigaChatSettings
from spi.gigachat_evidence import Block, EvidenceError
from spi.storage import Store


def make_table_pdf(path, caption, lines):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((45, 50), caption)
        for y, first_label, columns in lines:
            if first_label:
                page.insert_text((45, y), first_label)
            for x, value in columns:
                page.insert_text((x - pymupdf.get_text_length(value) / 2, y), value)
        pdf.save(path)
    return path


def rag_pdf(path):
    leaves = (130, 175, 220, 265, 310, 355, 400, 445)
    return make_table_pdf(
        path, "Table 1: Main results of multi-hop QA datasets. Best scores for the evaluated generators are shown.",
        [
            (66, "", [(197.5, "LLaMA3-8B-Instruct"), (377.5, "Qwen3.5")]),
            (79, "", list(zip((152.5, 242.5, 332.5, 422.5), ("HotpotQA", "2Wiki") * 2))),
            (92, "Method", list(zip(leaves, ("EM", "F1") * 4))),
            (108, "", [(290, "Non-Graph RAG")]),
            (122, "RA-CoT", list(zip(leaves, (".204", ".330", ".202", ".261", ".312", ".429", ".268", ".372")))),
            (137, "ReAct", list(zip(leaves, (".268", ".379", ".244", ".335", ".358", ".476", ".342", ".387")))),
        ],
    )


def rag_multilevel_pdf(path, first_model, second_model, *, axis="Method", axis_baseline=92, second_center=377.5,
                       caption="Table 1: We compare generators across QA datasets; retriever: Qwen-Embed"):
    def model_parts(parts, center):
        widths = [pymupdf.get_text_length(part) for part in parts]
        gap = 6.5
        left = center - (sum(widths) + gap * (len(parts) - 1)) / 2
        placed = []
        for part, width in zip(parts, widths, strict=True):
            placed.append((left + width / 2, part))
            left += width + gap
        return placed

    leaves = (130, 175, 220, 265, 310, 355, 400, 445)
    return make_table_pdf(path, caption, [
        (66, "", model_parts(first_model, 197.5) + model_parts(second_model, second_center)),
        (79, "", list(zip((152.5, 242.5, 332.5, 422.5), ("AtlasQA", "DeltaQA") * 2))),
        (axis_baseline, axis, []),
        (92, "", list(zip(leaves, ("EM", "F1") * 4))),
        (108, "GPT-4o mini", list(zip(leaves, (f".{100 + i}" for i in range(8))))),
        (123, "Graph RAG", list(zip(leaves, (f".{200 + i}" for i in range(8))))),
    ])


@pytest.mark.parametrize("second_model", [("Mistral v3-8B Instruct",), ("Gemini-2.5", "Flash")])
def test_rag_multilevel_generator_preserves_source_parts_and_roles(tmp_path, second_model):
    path = rag_multilevel_pdf(tmp_path / "D001.pdf", ("Gemini-2.5", "Flash"), second_model)
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = {result.raw.value: result for result in store.results("rag")}
    assert len(results) == 16
    for row, method in ((100, "GPT-4o mini"), (200, "Graph RAG")):
        for column in range(8):
            result = results[f".{row + column}"]
            model = ("Gemini-2.5", "Flash") if column < 4 else second_model
            dataset = ("AtlasQA", "DeltaQA")[column // 2 % 2]
            metric = ("EM", "F1")[column % 2]
            assert result.extraction_status == "verified"
            assert result.context["generator_model"] == " ".join(model)
            assert result.context["rag_variant"] == method
            assert result.context["retriever_model"] == "Qwen-Embed"
            assert result.context["dataset_name"] == dataset
            assert result.metric.name == metric
            assert result.context["header_path"] == [*model, dataset, metric]
    left = results[".100"].context["context_evidence"]["generator_model"][1]
    right = results[".104"].context["context_evidence"]["generator_model"][1]
    assert left["block_id"] != right["block_id"]


@pytest.mark.parametrize(("first_model", "second_model", "options"), [
    (("GPT-4o", "Gemini-2.5"), ("Claude-3.5 Sonnet",), {}),
    (("Qwen-Embedding v3",), ("Claude-3.5 Sonnet",), {}),
    (("Gemini-2.5", "Flash"), ("Claude-3.5 Sonnet",), {
        "second_center": 339,
        "caption": "Table 1: We evaluate methods across QA datasets; generator: Gemini-2.5 Flash; dataset: AtlasQA",
    }),
    (("Gemini-2.5 Flash",), ("Claude-3.5 Sonnet",), {
        "caption": "Table 1: We evaluate two retrievers on QA datasets with a fixed generator across all methods.",
    }),
    (("Gemini-2.5 Flash",), ("Claude-3.5 Sonnet",), {"axis": "Retriever"}),
    (("Gemini-2.5 Flash",), ("Claude-3.5 Sonnet",), {"axis": "Retriever model", "axis_baseline": 73}),
])
def test_rag_ambiguous_or_retriever_header_requires_review(tmp_path, first_model, second_model, options):
    path = rag_multilevel_pdf(tmp_path / "D001.pdf", first_model, second_model, **options)
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = {result.raw.value: result for result in store.results("rag")}
    assert len(results) == 16
    for column in range(8 if options else 4):
        result = results[f".{100 + column}"]
        assert result.extraction_status == "needs_review"
        assert "generator_model" not in result.context
        assert result.note == "rag_context_unresolved"
    if not options:
        assert results[".104"].extraction_status == "verified"


@pytest.mark.parametrize("comparison", [
    "We compare retrieval backbones across multiple QA datasets using EM and F1 scores.",
    "We evaluate reranking strategies across multiple QA datasets using EM and F1 scores.",
    "We compare prompt strategies across multiple QA datasets using EM and F1 scores.",
    "We compare methods across multiple QA datasets with a fixed generator and report EM and F1.",
])
def test_rag_qa_scores_do_not_make_non_generator_columns_generators(tmp_path, comparison):
    path = rag_multilevel_pdf(
        tmp_path / "D001.pdf", ("Gemini-2.5", "Flash"), ("Claude-3.5 Sonnet",),
        caption=f"Table 1: {comparison}",
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    assert len(results) == 16
    for result in results:
        assert result.extraction_status == "needs_review"
        assert result.note == "rag_context_unresolved"
        assert "generator_model" not in result.context
        assert result.context["dataset_name"] in {"AtlasQA", "DeltaQA"}
        assert result.context["rag_variant"] in {"GPT-4o mini", "Graph RAG"}


@pytest.mark.parametrize(("comparison", "missing_role"), [
    ("compare retrieval backbones", "retriever_model"),
    ("evaluate reranking models", "retrieval_method"),
    ("compare retrieval/reranking models", "retrieval_method"),
])
def test_rag_named_fixed_generator_requires_varying_retrieval_role(tmp_path, comparison, missing_role):
    path = rag_multilevel_pdf(
        tmp_path / "D001.pdf", ("Qwen-Embed",), ("BGE-Reranker",),
        caption=f"Table 1: We {comparison} on QA datasets (EM and F1); generator: Gemini-2.5 Flash",
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    assert len(results) == 16
    for result in results:
        assert result.extraction_status == "needs_review"
        assert result.note == "rag_context_unresolved"
        assert result.context["generator_model"] == "Gemini-2.5 Flash"
        assert result.context["header_path"][0] in {"Qwen-Embed", "BGE-Reranker"}
        assert missing_role in result.context["unconfirmed_context"]


@pytest.mark.parametrize(("comparison", "columns", "missing_role"), [
    ("compare prompt strategies on QA datasets", ("Zero-shot", "Few-shot"), "prompt_strategy"),
    ("compare methods on QA datasets with a fixed generator", ("Qwen3.5", "Claude-3.5 Sonnet"),
     "experiment_type"),
])
def test_rag_named_generator_cannot_verify_an_unconfirmed_column_role(tmp_path, comparison, columns, missing_role):
    path = rag_multilevel_pdf(
        tmp_path / "D001.pdf", (columns[0],), (columns[1],),
        caption=f"Table 1: We {comparison}; generator: Gemini-2.5 Flash",
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    assert len(results) == 16
    for result in results:
        assert result.extraction_status == "needs_review"
        assert result.note == "rag_context_unresolved"
        assert result.context["generator_model"] == "Gemini-2.5 Flash"
        assert result.context["rag_variant"] in {"GPT-4o mini", "Graph RAG"}
        assert result.context["dataset_name"] in {"AtlasQA", "DeltaQA"}
        assert result.context["header_path"][0] in columns
        assert missing_role in result.context["unconfirmed_context"]


def mock_model(monkeypatch, first_response):
    calls = []
    settings = GigaChatSettings(credentials=SecretStr("fixtureKey=="))

    def handle(request):
        if request.url.path.endswith("/oauth"):
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_at": int(time()) + 1800})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["max_tokens"] == 1800
        prompt, evidence = body["messages"][-1]["content"].split("\nPDF source:\n")
        assert "Omit facts or use an empty object" in prompt
        candidate = json.loads(prompt.rsplit("\n", 1)[1])
        assert "facts" not in candidate
        assert candidate["cell"] == json.loads(evidence)["table_cells"][0]["cell"]
        calls.append(candidate)
        response = first_response(candidate) if len(calls) == 1 else candidate
        return httpx.Response(200, json={
            "model": "GigaChat-3-Ultra:32.9.16.9",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"results": [response]})}}],
        })

    monkeypatch.setattr(batch, "GigaChatClient", lambda **options: GigaChatClient(
        settings, transport=httpx.MockTransport(handle), **options,
    ))
    return calls


@pytest.mark.parametrize(("changes", "issue"), [
    ({}, None),
    ({"raw_value": "0.204"}, "wrong_table_value_or_unit"),
    ({"raw_unit": "%"}, "wrong_table_value_or_unit"),
    ({"raw_value": "0.204", "raw_unit": "fraction"}, "wrong_table_value_or_unit"),
    ({"row": ["other-row"]}, "wrong_table_cell"),
    ({"headers": ["other-column"]}, "wrong_table_cell"),
    ({"block": "other-cell"}, "quote_mismatch"),
])
def test_rag_model_copies_pdf_cell_and_resumes(tmp_path, monkeypatch, changes, issue):
    path = rag_pdf(tmp_path / "D001.pdf")
    layout = Store(tmp_path / "layout")
    batch.run_batch(layout, [path], "rag", local_only=True)
    target, = (r for r in layout.results("rag")
               if r.context.get("rag_variant") == "RA-CoT" and r.context.get("dataset_name") == "HotpotQA"
               and r.context.get("generator_model") == "LLaMA3-8B-Instruct" and r.metric.name == "EM")
    cell_id = target.extraction_metadata.source_block_id
    calls = mock_model(monkeypatch, lambda candidate: dict(candidate, **changes))
    report = tmp_path / "model.json"

    def invoke():
        assert cli.main([
            str(path), "--domain", "rag", "--batch", "--state", str(tmp_path / "model"),
            "--report", str(report), "--max-requests", "1", "--completion-max-tokens", "1800",
            "--prioritize-cell", cell_id,
        ]) == 0
        return json.loads(report.read_text(encoding="utf-8"))

    first = invoke()
    assert len(calls) == 1 and calls[0]["cell"] == cell_id
    assert calls[0]["quote"] == calls[0]["raw_value"] == ".204"
    assert calls[0]["raw_unit"] == ""
    event = first["requests_this_run"][0]
    unit = first["documents"][0]["fragments"][event["fragment"]]
    assert event["finish_reason"] == "stop" and event["valid_json"]
    if issue:
        assert unit["issues"] == [issue] and unit["status"] == "review" and not unit["results"]
        if issue == "wrong_table_value_or_unit":
            assert unit["review_details"] == [{
                "code": issue, "source_block_id": cell_id,
                "fields": {key: {"proposed": value, "expected": calls[0][key]} for key, value in changes.items()},
            }]
    else:
        result, = unit["results"]
        assert result["extraction_status"] == "verified"
        assert result["raw"] == {"value": ".204", "unit": None}
        assert result["normalized"] == {"value": "0.204", "unit": None}
        assert result["source"]["page"] == 1
        assert result["source"]["row"] == "Non-Graph RAG / RA-CoT"
        assert result["source"]["column"] == "LLaMA3-8B-Instruct / HotpotQA / EM"

    resumed = invoke()
    assert len(calls) == 2 and calls[1]["cell"] != cell_id
    doc = resumed["documents"][0]
    assert doc["pending_units"] == first["documents"][0]["pending_units"] - 1
    assert doc["fragments"][event["fragment"]] == unit
    assert resumed["coverage"]["results"]["verified"] == (1 if issue else 2)


@pytest.mark.parametrize("facts", [None, {
    "device_label": "FB-SC", "material_name_raw": "reference film", "measurement_type": "photovoltaic performance",
}])
def test_solar_table_context_is_derived_and_conflicting_facts_stay_in_review(tmp_path, monkeypatch, facts):
    path = make_table_pdf(
        tmp_path / "D001.pdf",
        "Table 2: Summary of photovoltaic performance for champion devices based on the reference film.",
        [(74, "Direction", [(145, "Voc (mV)"), (230, "PCE (%)")]),
         (91, "FB-SC", [(145, "1,197"), (230, "15.6")]),
         (107, "SC-FB", [(145, "1,182"), (230, "15.2")])],
    )
    model = mock_model(monkeypatch, lambda candidate: candidate if facts is None else dict(candidate, facts=facts))
    store = Store(tmp_path / "state")
    first = batch.run_batch(store, [path], "solar", local_only=False, max_requests=1, completion_max_tokens=1800)
    assert len(model) == 1
    result, = store.results("solar")
    assert result.context["device_label"] == "the reference film"
    assert result.context["raw_scan_direction"] == "FB-SC"
    assert result.raw.value == "1,197" and result.raw.unit == "mV"
    if facts is None:
        assert result.extraction_status == "verified" and result.normalized.value == "1.197"
    else:
        assert result.extraction_status == "needs_review" and result.normalized.value is None
        assert set(first["coverage"]["review_reasons"]) == {
            "conflicting_fact:device_label", "unverified_fact_link:material_name_raw",
            "unverified_fact_link:measurement_type",
        }


@pytest.mark.parametrize(("value", "metric"), [("20.4", "EM"), ("-0.1", "F1"), (".204", "latency"), (".204", "BLEU")])
def test_unknown_rag_scale_requires_review(value, metric):
    with pytest.raises(EvidenceError, match="unknown_unit_or_scale"):
        normalize(value, "", metric, "rag")


@pytest.mark.parametrize(("header_text", "expected_error"), [
    (("Acc. (%)↑",), None),
    (("Acc.", "(%)↑"), None),
    (("Acc. (%) ↓",), None),
    (("Δ (%)↑",), "wrong_table_metric"),
])
def test_direction_arrow_preserves_the_explicit_unit(header_text, expected_error):
    caption = Block("caption", (0, 0, 1, 1), "Table 1: We evaluate two methods; generator: Model A; dataset: QA")
    row = Block("row", (0, 5, 1, 6), "Method A")
    headers = tuple(
        Block(f"h{i}", (5, 2 + i, 6, 3 + i), label)
        for i, label in enumerate(header_text)
    )
    value = Block("cell", (5, 5, 6, 6), "62.75")
    source = Fragment("table", 1, (caption, row, *headers, value), (Cell(value, (row,), headers),),
                      caption, "Table 1")
    proposal = Proposal(cell="cell", row=["row"], headers=[b.block_id for b in headers], block="cell",
                        quote=value.text, raw_value=value.text, raw_unit="%", metric="accuracy", origin="primary")

    if expected_error:
        with pytest.raises(EvidenceError, match=expected_error):
            verify(proposal, source, "rag", "rag:D001", "a" * 64, "local")
        return
    result, errors = verify(proposal, source, "rag", "rag:D001", "a" * 64, "local")
    assert not errors
    assert result.extraction_status == "verified"
    assert result.raw == result.normalized
    assert result.normalized.value == value.text and result.normalized.unit == "%"
    assert result.source.column == " / ".join(header_text)


def test_table_with_conflicting_explicit_units_cannot_verify():
    caption = Block("caption", (0, 0, 1, 1), "Table 1: Our measurements")
    row = Block("row", (0, 5, 1, 6), "Sample A")
    header = Block("header", (5, 3, 6, 4), "Voc (mV)")
    value = Block("cell", (5, 5, 6, 6), "1.197 V")
    source = Fragment("table", 1, (caption, row, header, value), (Cell(value, (row,), (header,)),),
                      caption, "Table 1")
    proposal = Proposal(cell="cell", row=["row"], headers=["header"], block="cell", quote="1.197 V",
                        raw_value="1.197", raw_unit="V", metric="Voc", origin="primary")
    with pytest.raises(EvidenceError, match="conflicting_units"):
        verify(proposal, source, "solar", "solar:D001", "a"*64, "GigaChat-3-Ultra")


def test_missing_prioritized_cell_never_calls_model(tmp_path):
    path = rag_pdf(tmp_path / "D001.pdf")
    response = batch.run_batch(
        Store(tmp_path / "state"), [path], "rag", local_only=False, max_requests=1,
        prioritize_cell="missing-id", complete=lambda *a, **kw: pytest.fail("unexpected model call"),
    )
    assert not response["requests_this_run"]
    document, = response["documents"]
    assert document["status"] == "failed"
    assert "match exactly one table cell" in document["errors"][0]["reason"]
