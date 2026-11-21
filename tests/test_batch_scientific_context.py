import pymupdf
import pytest

from spi import batch
from spi.batch_checks import quality_gate
from spi.models import Evidence, ExtractionMetadata, Metric, PublicationScope, ResultRecord, Value
from spi.storage import Store


def _pdf(path, caption, rows, *, prose=(), definition=None, definition_heading=None):
    with pymupdf.open() as pdf:
        if definition:
            page = pdf.new_page()
            if definition_heading:
                page.insert_text((40, 35), definition_heading, fontsize=10)
            page.insert_textbox((40, 45, 520, 120), definition, fontsize=9)
        page = pdf.new_page()
        page.insert_text((40, 45), caption, fontsize=9)
        for y, cells in rows:
            for x, label in cells:
                page.insert_text((x, y), label, fontsize=9)
        for y, paragraph in prose:
            page.insert_textbox((40, y, 525, y + 60), paragraph, fontsize=9)
        pdf.save(path)
    return path


RECALL = "recall@k is defined as the proportion of relevant documents returned among the top k results."


@pytest.mark.parametrize(("definition", "definition_heading", "section_reference", "verified"), [
    (None, None, "", False),
    (RECALL.capitalize(), None, "", False),
    ("For Table 12, " + RECALL, None, "", False),
    ("For Table 8, " + RECALL, None, "", True),
    (RECALL.capitalize(), "2.2 Performance Measurement", " (defined in Section 2.2)", True),
    (RECALL.capitalize(), "2.2 Performance Measurement", " (defined in Section 2.3)", False),
    ("For Table 12, " + RECALL, "2.2 Performance Measurement", " (defined in Section 2.2)", False),
    ("For a separate reranker experiment, " + RECALL, "2.2 Performance Measurement",
     " (defined in Section 2.2)", False),
])
def test_component_retrieval_uses_linked_prose_and_a_defined_scale(
    tmp_path, definition, definition_heading, section_reference, verified,
):
    path = _pdf(
        tmp_path / "component.pdf",
        "Table 8: Performance of semantic search for embedding models on ArchiveQA.",
        [(75, [(40, "Model type"), (145, "Recall@5"), (215, "Recall@10")]),
         (90, [(40, "Encoder-A"), (154, "0.41"), (225, "0.52")]),
         (105, [(40, "Encoder-B"), (154, "0.46"), (225, "0.59")])],
        prose=[
            (135, f"We evaluate embedding models on ArchiveQA using recall@k{section_reference}. "
                  "Table 8 reports our results."),
            (205, "In Table 12 we use generator Answer-7B and retrieval top-k: 20."),
        ],
        definition=definition,
        definition_heading=definition_heading,
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    result = next(r for r in store.results("rag") if r.raw.value == "0.52")
    assert result.metric.name == "recall" and result.metric.k == 10
    assert result.context["dataset_name"] == "ArchiveQA"
    assert result.context["retriever_model"] == "Encoder-A"
    assert "generator_model" not in result.context and "top_k" not in result.context
    assert result.raw.unit is None
    if verified:
        assert result.extraction_status == "verified" and result.normalized.unit == "fraction"
        assert result.source.origin == "primary"
        assert result.context["context_evidence"]["metric_scale"][0]["page"] == 1
    else:
        assert result.extraction_status == "needs_review"
        assert "metric_scale" in result.context["unconfirmed_context"]
        assert "metric_scale" not in result.context["context_evidence"]
        assert result.normalized.value is None


def test_calibration_mean_keeps_spread_and_delta_requires_a_metric(tmp_path):
    path = _pdf(
        tmp_path / "calibration.pdf",
        "Table 4: Comparison on AtlasQA using Mistral-7B. Values are mean +/- standard deviation.",
        [(75, [(40, "Method"), (165, "Acc. (%)"), (247, "ECE (%)"), (333, "Delta")]),
         (90, [(40, "Search-A"), (167, "82.0"), (244, "7.25 ± 0.5"), (333, "3.0")]),
         (105, [(40, "Search-B"), (167, "92.0"), (244, "6.25 ± 0.4"), (333, "4.0")])],
        prose=[(135, "For the comparison in Table 4, we use five seeds and report mean scores.")],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    assert len(results) == 4
    assert {r.metric.name for r in results} == {"accuracy", "ECE"}
    assert all(r.extraction_status == "verified" for r in results)
    assert all(r.context["generator_model"] == "Mistral-7B" and r.context["dataset_name"] == "AtlasQA" for r in results)
    assert all(r.context["aggregation"] == "mean" for r in results)
    assert {r.context["published_spread"] for r in results if r.metric.name == "ECE"} == {"± 0.5", "± 0.4"}
    document, = store.batch_documents("rag")
    details = [d for f in document["fragments"].values() for d in f.get("review_details", [])]
    assert len(details) == 2
    assert all(d["code"] == "unknown_metric" and d["headers"][0]["text"] == "Delta"
               and d["pdf_sha256"] == document["sha256"] for d in details)


def test_scan_rows_and_ipce_are_distinct_and_caption_citation_blocks_origin(tmp_path):
    path = _pdf(
        tmp_path / "photovoltaic.pdf",
        "Table 3: Photovoltaic measurements of the test devices [12]",
        [(75, [(40, "HTM"), (125, "Scan direction"), (239, "PCE (%)"), (321, "Integrated Jsc from IPCE (mA/cm2)")]),
         (90, [(40, "Film A"), (139, "forward"), (246, "12.5"), (375, "19.0")]),
         (104, [(139, "reverse"), (246, "13.1")]),
         (118, [(40, "Film B"), (139, "forward"), (246, "15.6"), (375, "20.0")]),
         (132, [(139, "reverse"), (246, "16.2")])],
        prose=[(160, "We report J-V measurements for the fabricated devices in Table 3.")],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "solar", local_only=True)
    results = store.results("solar")
    assert len(results) == 6 and all(r.extraction_status == "needs_review" for r in results)
    reverse = next(r for r in results if r.raw.value == "13.1")
    assert reverse.context["device_label"] == "Film A" and reverse.context["scan_direction"] == "reverse"
    assert reverse.context["measurement_type"] == "J-V"
    assert "material_name_raw" not in reverse.context
    assert "origin" in reverse.context["unconfirmed_context"]
    spectral = next(r for r in results if r.raw.value == "19.0")
    assert spectral.metric.name == "Jsc" and spectral.context["measurement_type"] == "EQE"
    assert "scan_direction" not in spectral.context


@pytest.mark.parametrize(("domain", "caption", "axis", "headings", "missing"), [
    ("rag", "Table 1: We evaluate on ArchiveQA using Llama-8B.",
     "Model type", ("Accuracy (%)", "ECE (%)"), "rag_variant"),
    ("solar", "Table 1: Our measurements of optical transmission and reflection.",
     "Sample", ("Efficiency (%)", "Other (%)"), "metric"),
])
def test_numeric_cells_do_not_establish_missing_scientific_roles(tmp_path, domain, caption, axis, headings, missing):
    path = _pdf(
        tmp_path / "context.pdf", caption,
        [(75, [(40, axis), (145, headings[0]), (225, headings[1])]),
         (90, [(40, "System-A"), (161, "45.0"), (233, "10.1")]),
         (105, [(40, "System-B"), (161, "46.1"), (233, "12.4")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], domain, local_only=True)
    results = store.results(domain)
    assert results and all(r.extraction_status == "needs_review" for r in results)
    assert all(missing in r.context["unconfirmed_context"] for r in results)


@pytest.mark.parametrize("comparison", ["retrievers", "rerankers", "opinion extraction models"])
def test_row_metric_preserves_header_and_first_row_without_assigning_model_roles(tmp_path, comparison):
    path = _pdf(
        tmp_path / "row-metric.pdf",
        f"Table 2: We compare {comparison}; generator: Answer-7B; dataset: ArchiveQA",
        [(75, [(40, "Metric"), (145, "Mistral-7B"), (245, "Gemini-2.5")]),
         (90, [(40, "Accuracy (%)"), (158, "84.0"), (256, "85.0*")]),
         (105, [(40, "Recall@5"), (158, "0.41"), (256, "0.52")]),
         (120, [(40, "Coverage"), (158, "26.0"), (256, "35.0")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    first, left, right = sorted(store.results("rag"), key=lambda r: r.metric.name + r.raw.value)
    assert first.raw.value == "84.0" and first.raw.unit == "%"
    assert first.context["header_path"] == ["Mistral-7B"]
    assert first.source.row == "Accuracy (%)" and first.source.column == "Mistral-7B"
    assert first.source.fragment == "84.0" and first.source.origin == "primary"
    assert [b["text"] for b in first.context["context_evidence"]["metric"]] == ["Accuracy (%)", "Metric"]
    for result in (first, left, right):
        assert result.extraction_status == "needs_review"
        assert result.context["generator_model"] == "Answer-7B"
        assert result.context["dataset_name"] == "ArchiveQA"
        assert {"retriever_model", "retrieval_method", "rag_variant"}.isdisjoint(result.context)
        assert "column_role" in result.context["unconfirmed_context"]
    for result in (left, right):
        assert result.metric.name == "recall" and result.metric.k == 5
        assert result.raw.unit is None and result.normalized.value is None
        assert "metric_scale" in result.context["unconfirmed_context"]
    document, = store.batch_documents("rag")
    details = {d["value"]["text"]: d for f in document["fragments"].values() for d in f.get("review_details", [])}
    assert details["85.0*"]["code"] == "unsupported_number"
    assert details["85.0*"]["headers"][0]["text"] == "Gemini-2.5"
    assert details["26.0"]["code"] == "unknown_metric"


@pytest.mark.parametrize("parent_x", [177, 220])
def test_row_metric_requires_unambiguous_multilevel_columns(tmp_path, parent_x):
    path = _pdf(
        tmp_path / "groups.pdf",
        "Table 3: We compare retrieval methods on ArchiveQA with raw and enriched indexing in each experiment.",
        [(75, [(parent_x, "Semantic"), (355, "Hybrid")]),
         (90, [(40, "Metric"), (145, "Raw"), (225, "Enriched"), (315, "Raw"), (395, "Enriched")]),
         (105, [(40, "F1"), (145, "0.45"), (230, "0.52"), (315, "0.55"), (400, "0.56")]),
         (120, [(40, "EM"), (145, "0.35"), (230, "0.42"), (315, "0.43"), (400, "0.51")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    if parent_x == 177:
        assert len(results) == 8 and all(r.extraction_status == "needs_review" for r in results)
        assert next(r for r in results if r.raw.value == "0.55").source.column == "Hybrid / Raw"
        assert next(r for r in results if r.raw.value == "0.52").context["header_path"] == ["Semantic", "Enriched"]
    else:
        assert not results
        document, = store.batch_documents("rag")
        assert [issue["reason"] for issue in document["layout_issues"]] == ["table_layout_unresolved"]


def test_conflicting_row_metric_and_column_statistic_remain_unresolved(tmp_path):
    path = _pdf(
        tmp_path / "mixed.pdf",
        "Table 4: We compare systems on ArchiveQA and report raw scores and their differences.",
        [(75, [(40, "Metric"), (145, "System-A (v2)"), (245, "Accuracy"), (345, "Delta")]),
         (90, [(40, "F1"), (164, "0.45"), (254, "0.51"), (352, "0.06")]),
         (105, [(40, "EM"), (164, "0.35"), (254, "0.39"), (352, "0.04")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    results = store.results("rag")
    assert {r.raw.value for r in results} == {"0.45", "0.35"}
    assert all(r.raw.unit is None and r.extraction_status == "needs_review" for r in results)
    document, = store.batch_documents("rag")
    details = [d for f in document["fragments"].values() for d in f.get("review_details", [])]
    assert len(details) == 4 and all(d["code"] == "ambiguous_header" for d in details)


@pytest.mark.parametrize("axis", ["Metric", "System"])
def test_unaligned_first_value_cannot_become_a_header(tmp_path, axis):
    path = _pdf(
        tmp_path / "misaligned.pdf",
        "Table 5: We evaluate on ArchiveQA using Llama-8B and report the individual comparison scores.",
        [(75, [(40, axis), (145, "Alpha"), (220, "Beta"), (295, "Gamma")]),
         (90, [(40, "F1"), (145, "0.44"), (220, "0.51"), (330 if axis == "Metric" else 295, "0.59*")]),
         (105, [(40, "EM"), (145, "0.34"), (220, "0.41"), (295, "0.43")]),
         (120, [(40, "Accuracy"), (145, "0.36"), (220, "0.48"), (295, "0.53")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "rag", local_only=True)
    assert not store.results("rag")
    document, = store.batch_documents("rag")
    assert [issue["reason"] for issue in document["layout_issues"]] == ["table_layout_unresolved"]


def test_quality_gate_zero_results_can_coexist_with_structural_export(tmp_path):
    from spi.csv_export import validate_submission
    from spi.csv_schema import load_schema
    from spi.store_export import project_submission

    path = _pdf(
        tmp_path / "Device.pdf", "Table 9: Unresolved results for sets of experimental devices.",
        [(75, [(40, "Sample"), (145, "Delta"), (215, "(%)")]),
         (90, [(40, "A"), (151, "0.4"), (216, "0.5")]),
         (105, [(40, "B"), (151, "0.6"), (216, "0.7")])],
    )
    store = Store(tmp_path / "state")
    batch.run_batch(store, [path], "solar", local_only=True)
    document, = store.documents()
    store.save_publication_scopes([PublicationScope(
        document_id=document["document_id"], pdf_sha256=document["sha256"], status="in_scope",
        reason="Synthetic scope decision", source_pages=[1],
    )])
    schema = load_schema("solar")
    submission = project_submission(store, schema, tmp_path, publication_ids=[path.stem])
    validate_submission(schema, submission, publication_ids=[path.stem])
    assert submission.quality_gate["status"] == "NO-GO"
    assert submission.quality_gate["in_scope_publications"] == 1
    assert submission.quality_gate["publications_without_verified_results"] == [path.stem]
    assert submission.quality_gate["verified_results"] == 0
    assert submission.quality_gate["unresolved"] == 4
    details = [d for f in store.batch_documents("solar")[0]["fragments"].values() for d in f["review_details"]]
    assert {d["code"] for d in details} == {"ambiguous_header", "unknown_metric"}


def test_quality_gate_excludes_results_and_checkpoints_from_another_pdf_version():
    document_id = "solar:Versioned"
    current_hash, old_hash = "a" * 64, "b" * 64
    stale = ResultRecord(
        result_id="old", document_id=document_id, domain="solar", metric=Metric(name="PCE"),
        raw=Value(value="11.5", unit="%"), normalized=Value(value="11.5", unit="%"),
        source=Evidence(page=1, table="Table 1", fragment="11.5", origin="primary"),
        extraction_status="verified",
        extraction_metadata=ExtractionMetadata(
            model_id="local", method="pymupdf_verified_table", pdf_sha256=old_hash,
            schema_version="test", profile="solar",
        ),
    )
    stale_review = stale.model_copy(update={"result_id": "old-review", "extraction_status": "needs_review"})
    checkpoints = [{"document_id": document_id, "sha256": old_hash, "pending_units": 5}]
    in_scope = {document_id: current_hash}

    gate = quality_gate(in_scope, [stale, stale_review], checkpoints)
    assert gate["status"] == "NO-GO"
    assert gate["publications_without_verified_results"] == ["Versioned"]
    assert gate["verified_results"] == gate["needs_review"] == gate["unresolved"] == 0

    current = stale.model_copy(update={
        "result_id": "current",
        "extraction_metadata": stale.extraction_metadata.model_copy(update={"pdf_sha256": current_hash}),
    })
    gate = quality_gate(in_scope, [stale, stale_review, current], checkpoints)
    assert gate["status"] == "GO" and gate["publications_with_verified_results"] == 1
    assert gate["verified_results"] == 1 and gate["needs_review"] == gate["unresolved"] == 0
