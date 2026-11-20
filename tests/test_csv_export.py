import csv
import json
from decimal import Decimal

import pytest

from spi.csv_export import Submission, SubmissionError, export_submission, read_submission, validate_submission
from spi.csv_schema import SchemaError, load_schema


TABLES = {
    "solar": ("publications", "materials", "devices", "layers", "measurements", "results", "issues"),
    "rag": ("publications", "configurations", "datasets", "tasks", "experiments", "results", "issues"),
}


@pytest.fixture(params=["solar", "rag"])
def submission(request):
    domain = request.param
    tables = {name: [] for name in TABLES[domain]}
    tables["publications"] = [
        {"publication_id": "D001", "title": "Тестовая публикация", "year": None, "in_scope": True},
        {"publication_id": "D002", "in_scope": False, "exclusion_reason": "Вне предметной области"},
    ]
    result = dict(publication_id="D001", result_id="r1", result_origin="primary",
                  extraction_status="extracted", metric_name="test fixture",
                  raw_value="0,50", raw_unit="доля", metric_value=Decimal("0.50"),
                  normalized_value=Decimal("0.50"), metric_unit="fraction", normalized_unit="fraction",
                  source_page=1, source_table="Table 1", source_fragment='Тест, "цитата"\rстрока')
    if domain == "solar":
        tables["materials"] = [{"publication_id": "D001", "material_id": "m1"}]
        tables["devices"] = [
            {"publication_id": "D001", "device_id": "d1", "absorber_material_id": "m1"},
            {"publication_id": "D001", "device_id": "d2"},
        ]
        tables["layers"] = [{"publication_id": "D001", "layer_id": "l1", "device_id": "d1"}]
        tables["measurements"] = [
            {"publication_id": "D001", "measurement_id": "e1", "device_id": "d1; d2", "mask_used": False}
        ]
        result.update(device_id="d1", measurement_id="e1", material_id="m1")
    else:
        for table, key in (("experiments", "experiment_id"), ("configurations", "config_id"),
                           ("datasets", "dataset_id"), ("tasks", "task_id")):
            tables[table] = [{"publication_id": "D001", key: "id1"}]
            result[key] = "id1"
        result["metric_k"] = 0
    tables["results"] = [result]
    tables["issues"] = [
        {"publication_id": "D001", "issue_id": "i1", "record_ids": "r1", "check_name": "test fixture"}
    ]
    return Submission(domain, tables)


@pytest.mark.parametrize("domain", TABLES)
def test_template_loading_and_column_order(tmp_path, domain):
    schema = load_schema(domain)
    assert tuple(schema.tables) == TABLES[domain]
    assert schema.tables["publications"].columns == (
        "publication_id", "title", "year", "DOI", "authors", "in_scope", "exclusion_reason"
    )
    template = tmp_path / "submission_template"
    template.mkdir()
    for name, table in schema.tables.items():
        (template / f"{name}.csv").write_text(",".join(table.columns) + "\n", encoding="utf-8-sig")
    assert load_schema(domain, tmp_path) == schema
    publications = template / "publications.csv"
    publications.write_text("title,publication_id,year,DOI,authors,in_scope,exclusion_reason\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="column order"):
        load_schema(domain, template)


def test_missing_templates_and_rag_manifest(tmp_path):
    with pytest.raises(SchemaError, match="Cannot read template.*publications"):
        load_schema("solar", tmp_path / "missing")
    schema = load_schema("rag")
    template = export_submission(schema, Submission("rag", {name: [] for name in schema.tables}),
                                 tmp_path / "template", publication_ids=[])
    manifest = {
        "version": "rag-viniti-adaptation-1.0",
        "official_rag_template": False,
        "tables": {name: list(table.columns) for name, table in schema.tables.items()},
        "keys": dict(publications="publication_id", configurations="config_id", datasets="dataset_id",
                     tasks="task_id", experiments="experiment_id", results="result_id", issues="issue_id"),
        "enums": {
            "publications.in_scope": ["true", "false"],
            "results.result_origin": ["primary", "cited"],
            "results.extraction_status": ["extracted", "present_not_extractable", "absent"],
        },
    }
    file = template / "schema.json"
    file.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_schema("rag", template) == schema
    manifest["official_rag_template"] = True
    file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SchemaError, match="differs from rag-viniti-adaptation"):
        load_schema("rag", template)


def test_export_utf8_null_and_raw_values(tmp_path, submission):
    schema = load_schema(submission.domain)
    directory = export_submission(schema, submission, tmp_path / "submission", publication_ids=["D001", "D002"])
    assert {path.name for path in directory.iterdir()} == {f"{name}.csv" for name in TABLES[submission.domain]}
    assert "Тестовая публикация".encode() in (directory / "publications.csv").read_bytes()
    with (directory / "results.csv").open(encoding="utf-8", newline="") as file:
        assert tuple(next(csv.reader(file))) == schema.tables["results"].columns
    imported = read_submission(schema, directory)
    validate_submission(schema, imported, publication_ids=["D001", "D002"])
    first, excluded = imported.tables["publications"]
    assert first["year"] is first["DOI"] is first["exclusion_reason"] is None
    assert excluded["in_scope"] == "false"
    result = imported.tables["results"][0]
    assert (result["raw_value"], result["raw_unit"]) == ("0,50", "доля")
    assert (result["normalized_value"], result["normalized_unit"]) == ("0.50", "fraction")
    assert result["source_fragment"] == 'Тест, "цитата"\rстрока'
    if submission.domain == "rag":
        assert result["metric_k"] == "0"
    with pytest.raises(FileExistsError):
        export_submission(schema, submission, directory, publication_ids=["D001", "D002"])


def test_primary_keys_are_scoped_to_the_publication(submission):
    schema = load_schema(submission.domain)
    ids = ["D001", "D002", "D003"]
    submission.tables["publications"].append({"publication_id": "D003", "in_scope": True})
    table = "devices" if submission.domain == "solar" else "configurations"
    existing = submission.tables[table][-1]
    submission.tables[table].append({**existing, "publication_id": "D003"})
    validate_submission(schema, submission, publication_ids=ids)
    submission.tables[table].append(existing)
    with pytest.raises(SubmissionError, match="duplicate key"):
        validate_submission(schema, submission, publication_ids=ids)


def test_dangling_and_cross_publication_references(submission):
    schema = load_schema(submission.domain)
    ids = ["D001", "D002", "D003"]
    submission.tables["publications"].append({"publication_id": "D003", "in_scope": True})
    table = "measurements" if submission.domain == "solar" else "experiments"
    submission.tables[table][0]["publication_id"] = "D003"
    if submission.domain == "solar":
        submission.tables[table][0]["device_id"] = None
    with pytest.raises(SubmissionError, match="does not exist.*for D001"):
        validate_submission(schema, submission, publication_ids=ids)


def test_exclusions_and_pdf_coverage(submission):
    schema = load_schema(submission.domain)
    with pytest.raises(SubmissionError, match="missing.*D003"):
        validate_submission(schema, submission, publication_ids=["D001", "D002", "D003"])
    submission.tables["publications"][1]["exclusion_reason"] = ""
    with pytest.raises(SubmissionError, match="exclusion_reason"):
        validate_submission(schema, submission, publication_ids=["D001", "D002"])


def test_mixed_tracks_and_incomplete_bundle(tmp_path, submission):
    other = "rag" if submission.domain == "solar" else "solar"
    schema = load_schema(submission.domain)
    with pytest.raises(SubmissionError, match="cannot use"):
        validate_submission(load_schema(other), submission, publication_ids=["D001", "D002"])
    directory = export_submission(schema, submission, tmp_path / "submission", publication_ids=["D001", "D002"])
    other_only = "devices" if other == "solar" else "configurations"
    (directory / f"{other_only}.csv").write_text("unexpected", encoding="utf-8")
    with pytest.raises(SubmissionError, match=f"unexpected.*{other_only}"):
        read_submission(schema, directory)
    del submission.tables["issues"]
    with pytest.raises(SubmissionError, match="missing.*issues"):
        validate_submission(schema, submission, publication_ids=["D001", "D002"])


def test_invalid_evidence_never_leaves_partial_export(tmp_path, submission):
    schema = load_schema(submission.domain)
    result = submission.tables["results"][0]
    result["source_page"] = 0
    destination = tmp_path / "failed"
    with pytest.raises(SubmissionError, match="source_page must be positive"):
        export_submission(schema, submission, destination, publication_ids=["D001", "D002"])
    assert not destination.exists()
    result.update(source_page=1, raw_value=None)
    with pytest.raises(SubmissionError, match="requires raw_value"):
        validate_submission(schema, submission, publication_ids=["D001", "D002"])


@pytest.mark.parametrize("submission", ["rag"], indirect=True)
@pytest.mark.parametrize(("fragment", "table", "figure"), [(None, "Table 1", None), (" \n", None, "Fig. 1")])
def test_rag_extracted_requires_fragment_with_table_or_figure(submission, fragment, table, figure):
    submission.tables["results"][0].update(
        source_fragment=fragment, source_table=table, source_figure=figure
    )
    with pytest.raises(SubmissionError, match="RAG extracted result requires source_fragment"):
        validate_submission(load_schema("rag"), submission, publication_ids=["D001", "D002"])


@pytest.mark.parametrize("submission", ["solar"], indirect=True)
def test_solar_rejects_device_from_a_different_measurement(submission):
    submission.tables["results"][0]["device_id"] = "d2"
    submission.tables["measurements"][0]["device_id"] = "d1"
    with pytest.raises(SubmissionError, match="device_id='d2'.*measurement_id='e1'"):
        validate_submission(load_schema("solar"), submission, publication_ids=["D001", "D002"])


@pytest.mark.parametrize("submission", ["solar"], indirect=True)
@pytest.mark.parametrize(
    ("device_id", "measurement_id", "measurement_devices"),
    [("d2", "e1", "d1; d2"), (None, "e1", "d1"), ("d2", None, "d1"), ("d2", "e1", None)],
)
def test_solar_allows_matching_or_unspecified_measurement_link(
    submission, device_id, measurement_id, measurement_devices
):
    submission.tables["results"][0].update(
        device_id=device_id, measurement_id=measurement_id, source_fragment=None
    )
    submission.tables["measurements"][0]["device_id"] = measurement_devices
    validate_submission(load_schema("solar"), submission, publication_ids=["D001", "D002"])


@pytest.mark.parametrize("status", ["needs_review", "verified"])
def test_csv_requires_explicit_submission_status(submission, status):
    submission.tables["results"][0]["extraction_status"] = status
    with pytest.raises(SubmissionError, match=f"invalid extraction_status.*{status}"):
        validate_submission(load_schema(submission.domain), submission, publication_ids=["D001", "D002"])


def test_result_duplicate_cycle(submission):
    result = submission.tables["results"][0]
    result["duplicate_of"] = result["result_id"]
    with pytest.raises(SubmissionError, match="duplicate_of cycle"):
        validate_submission(load_schema(submission.domain), submission, publication_ids=["D001", "D002"])
