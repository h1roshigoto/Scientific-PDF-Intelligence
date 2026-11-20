"""Synthetic PDFs and mocked completions; the live D001 run is a separate CLI check."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from spi import gigachat_pipeline as pipeline
from spi.api import app
from spi.csv_export import SubmissionError, export_submission, read_submission
from spi.csv_schema import load_schema
from spi.gigachat import Completion, GigaChatError
from spi.gigachat_evidence import Block, Source
from spi.models import PublicationScope
from spi.storage import Store
from spi.store_export import project_submission


MODEL = "GigaChat-3-Ultra:mock"


def record_scope(store, status, reason=None):
    doc, = store.documents()
    scope = PublicationScope(document_id=doc["document_id"], pdf_sha256=doc["sha256"],
                             status=status, reason=reason, source_pages=[1] if reason else [])
    store.save_publication_scopes([scope])


@pytest.fixture(params=pipeline.PROFILES, ids=("solar", "rag"))
def case(request, tmp_path, monkeypatch):
    profile = request.param
    lookup = profile.lookup
    path = tmp_path / "D001.pdf"
    with pymupdf.open() as pdf:
        for _ in range(profile.page):
            pdf.new_page()
        pdf[-1].insert_text((40, 40), "Synthetic table")
        pdf.save(path)
    cell = Block("target", (60, 70, 80, 90), "812" if lookup.unit else ".321")
    distractor = Block("other", (85, 70, 105, 90), cell.text)
    caption = Block("caption", (40, 10, 90, 30), "Table 1: Main results of multi-hop QA datasets.")
    source = Source(profile.page, (caption, distractor, cell), cell)
    monkeypatch.setattr(pipeline, "prepare_source", lambda *args, **kwargs: source)
    response = {
        "raw_value": cell.text, "raw_unit": lookup.unit, "object_or_method": lookup.subject,
        "metric": lookup.metric, "context": f"Table 1, {lookup.model}, {lookup.dataset}",
        "source_block_id": cell.block_id, "source_quote": cell.text,
    }
    return Store(tmp_path / "state"), path, profile, response


def test_verified_result_cache_api_and_export(case, monkeypatch):
    store, pdf, profile, response = case
    complete = Mock(return_value=Completion(model_id=MODEL, content=json.dumps(response)))
    assert pipeline.process_pdf(store, pdf, profile, complete)["status"] == "verified"
    record_scope(store, "in_scope", "Synthetic article satisfies the track criteria.")
    assert pipeline.process_pdf(store, pdf, profile, complete)["cache_hit"]
    complete.assert_called_once()
    assert complete.call_args.kwargs["schema"]["additionalProperties"] is False
    assert len(store.documents()) == len(store.results()) == len(store.read(pipeline.CACHE_FILE)) == 1

    monkeypatch.setenv("SPI_STATE_DIR", str(store.root))
    client = TestClient(app)
    result = client.get(f"/results?domain={profile.domain}").json()[0]
    assert client.get(f"/results/{result['result_id']}").json() == result
    assert result["raw"]["value"] == response["raw_value"]
    assert result["normalized"]["value"] == ("0.812" if profile.domain == "solar" else "0.321")
    assert result["source"]["page"] == profile.page
    assert result["extraction_metadata"]["model_id"] == MODEL
    original = client.get(f"/documents/{result['document_id']}/file#page={profile.page}")
    assert original.content == pdf.read_bytes()
    assert original.headers["content-disposition"].startswith("inline")

    with pymupdf.open() as other:
        other.new_page().insert_text((40, 40), "Unimported synthetic article")
        other.save(pdf.with_name("D002.pdf"))
    schema = load_schema(profile.domain)
    submission = project_submission(store, schema, pdf.parent, publication_ids=["D001"])
    with pytest.raises(SubmissionError, match="D002 has not been imported"):
        project_submission(store, schema, pdf.parent, publication_ids=["D001", "D002"])
    destination = export_submission(schema, submission, pdf.parent / "csv", publication_ids=["D001"])
    rows = read_submission(schema, destination).tables
    assert len(rows) == 7
    publication, = rows["publications"]
    assert publication["publication_id"] == "D001"
    assert publication["in_scope"] == "true"
    assert publication["exclusion_reason"] is None
    exported = rows["results"][0]
    assert exported["extraction_status"] == "extracted"
    assert exported["raw_value"] == response["raw_value"]
    assert exported["source_fragment"] == response["source_quote"]
    for reference in schema.tables["results"].references:
        if exported[reference.column]:
            assert rows[reference.target][0][schema.tables[reference.target].key] == exported[reference.column]

    revised = pdf.with_name("revised.pdf")
    with pymupdf.open(pdf) as document:
        document[-1].insert_text((40, 60), "Revision")
        document.save(revised)
    revised.replace(pdf)
    with pytest.raises(SubmissionError, match="does not match the imported SHA-256"):
        project_submission(store, schema, pdf.parent, publication_ids=["D001"])
    assert not pipeline.process_pdf(store, pdf, profile, complete)["cache_hit"]
    with pytest.raises(SubmissionError, match="scope needs_review"):
        project_submission(store, schema, pdf.parent, publication_ids=["D001"])
    monkeypatch.setattr(pipeline, "SCHEMA_VERSION", "test-new-version")
    assert not pipeline.process_pdf(store, pdf, profile, complete)["cache_hit"]
    assert not pipeline.process_pdf(store, pdf, replace(profile, name="new-profile"), complete)["cache_hit"]
    assert complete.call_count == len(store.read(pipeline.CACHE_FILE)) == 4
    assert len(store.results()) == 1


def test_excluded_publication_has_a_reason_and_no_dependent_rows(case):
    store, pdf, profile, response = case
    complete = Mock(return_value=Completion(model_id=MODEL, content=json.dumps(response)))
    assert pipeline.process_pdf(store, pdf, profile, complete)["status"] == "verified"
    with pytest.raises(ValidationError, match="requires a reason"):
        record_scope(store, "out_of_scope")
    reason = "Synthetic article describes a technology outside the track criteria."
    record_scope(store, "out_of_scope", reason)

    schema = load_schema(profile.domain)
    submission = project_submission(store, schema, pdf.parent, publication_ids=["D001"])
    destination = export_submission(schema, submission, pdf.parent / "csv", publication_ids=["D001"])
    rows = read_submission(schema, destination).tables
    publication, = rows["publications"]
    assert publication["in_scope"] == "false"
    assert publication["exclusion_reason"] == reason
    assert all(not table for name, table in rows.items() if name != "publications")
    assert store.results()[0].extraction_status == "verified"


def test_pending_scope_blocks_export_even_with_a_verified_result(case):
    store, pdf, profile, response = case
    complete = Mock(return_value=Completion(model_id=MODEL, content=json.dumps(response)))
    assert pipeline.process_pdf(store, pdf, profile, complete)["status"] == "verified"
    schema = load_schema(profile.domain)
    with pytest.raises(SubmissionError, match="scope needs_review"):
        project_submission(store, schema, pdf.parent, publication_ids=["D001"])

    record_scope(store, "needs_review")
    decision, = store.publication_scopes().values()
    assert decision.status == "needs_review"
    with pytest.raises(SubmissionError, match="scope needs_review"):
        project_submission(store, schema, pdf.parent, publication_ids=["D001"])


def test_wrong_cell_is_neither_cached_nor_exported(case):
    store, pdf, profile, response = case
    response["source_block_id"] = "other"
    complete = Mock(return_value=Completion(model_id=MODEL, content=json.dumps(response)))
    assert pipeline.process_pdf(store, pdf, profile, complete)["error"] == "wrong_table_cell"
    result, = store.results()
    assert result.extraction_status == "needs_review"
    assert result.normalized.value is None
    assert result.source.fragment is None
    assert store.read(pipeline.CACHE_FILE) == []
    record_scope(store, "in_scope", "Synthetic article satisfies the track criteria.")
    submission = project_submission(store, load_schema(profile.domain), pdf.parent, publication_ids=["D001"])
    assert submission.tables["results"] == []


def test_missing_key_network_and_schema_errors_create_no_results(case, monkeypatch):
    store, pdf, profile, response = case
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "")
    assert pipeline.process_pdf(store, pdf, profile, lambda *a, **kw: pipeline.GigaChatClient())["error"] == "missing_credentials"
    timeout = Mock(side_effect=GigaChatError("completion", "connect_timeout"))
    assert pipeline.process_pdf(store, pdf, profile, timeout)["error"] == "connect_timeout"
    response["invented_field"] = "not allowed"
    invalid = Mock(return_value=Completion(model_id=MODEL, content=json.dumps(response)))
    assert pipeline.process_pdf(store, pdf, profile, invalid)["error"] == "invalid_schema"
    assert store.results() == store.read(pipeline.CACHE_FILE) == []
