"""Export SPI records to the seven participant CSV files for a track."""

import argparse
from hashlib import sha256
from pathlib import Path

from .csv_export import Submission, SubmissionError, export_submission
from .csv_schema import SubmissionSchema, load_schema
from .models import ResultRecord
from .storage import Store


def _related_rows(result: ResultRecord, tables: dict, publication_id: str) -> dict:
    prefix = dict(publication_id=publication_id)
    context = result.context
    if result.domain == "solar":
        device_id = f"{result.result_id}:device"
        measurement_id = f"{result.result_id}:measurement"
        tables["devices"].append(dict(prefix, device_id=device_id, device_label=context["device_label"]))
        tables["measurements"].append(dict(prefix, measurement_id=measurement_id, device_id=device_id,
                                           measurement_type=context.get("measurement_type")))
        return {"device_id": device_id, "measurement_id": measurement_id}

    config_id = f"{result.result_id}:config"
    dataset_id = f"{result.result_id}:dataset"
    task_id = f"{result.result_id}:task"
    experiment_id = f"{result.result_id}:experiment"
    tables["configurations"].append(dict(prefix, config_id=config_id, rag_variant=context["rag_variant"],
                                         generator_model=context["generator_model"]))
    tables["datasets"].append(dict(prefix, dataset_id=dataset_id, dataset_name=context["dataset_name"]))
    tables["tasks"].append(dict(prefix, task_id=task_id, task_type=context["task_type"]))
    tables["experiments"].append(dict(prefix, experiment_id=experiment_id,
                                      note=f"{result.source.table}, PDF page {result.source.page}"))
    return {"config_id": config_id, "dataset_id": dataset_id, "task_id": task_id, "experiment_id": experiment_id}


def project_submission(store: Store, schema: SubmissionSchema, pdf_dir: Path) -> Submission:
    tables = {name: [] for name in schema.tables}
    pdfs = sorted(path for path in pdf_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
    if not pdfs:
        raise SubmissionError(f"No PDFs in {pdf_dir}")
    documents = {d["publication_id"]: d for d in store.documents() if d["domain"] == schema.domain}
    publications = {}
    for pdf in pdfs:
        doc = documents.get(pdf.stem)
        if doc is None or doc["sha256"] != sha256(pdf.read_bytes()).hexdigest():
            raise SubmissionError(f"SPI has no matching import for {schema.domain}:{pdf.stem}")
        publications[doc["document_id"]] = doc
        tables["publications"].append({"publication_id": pdf.stem, "in_scope": True})

    for result in store.results(schema.domain):
        doc = publications.get(result.document_id)
        metadata = result.extraction_metadata
        if (doc is None or result.extraction_status != "verified" or metadata is None
                or metadata.pdf_sha256 != doc["sha256"]):
            continue
        pid = doc["publication_id"]
        source = result.source
        tables["results"].append({
            **_related_rows(result, tables, pid),
            "publication_id": pid, "result_id": result.result_id,
            "metric_name": result.metric.name, "metric_value": result.normalized.value,
            "metric_unit": result.normalized.unit, "raw_value": result.raw.value, "raw_unit": result.raw.unit,
            "normalized_value": result.normalized.value, "normalized_unit": result.normalized.unit,
            "result_origin": source.origin, "cited_publication_id": source.cited_publication_id,
            "source_page": source.page, "source_section": source.section, "source_table": source.table,
            "source_figure": source.figure, "source_fragment": source.fragment, "extraction_status": "extracted",
        })
    return Submission(schema.domain, tables)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", choices=("solar", "rag"))
    parser.add_argument("output", type=Path, help="New submission directory")
    parser.add_argument("--pdf-dir", required=True, type=Path)
    parser.add_argument("--state", default="state")
    parser.add_argument("--template-dir", type=Path)
    args = parser.parse_args()
    try:
        schema = load_schema(args.domain, args.template_dir)
        submission = project_submission(Store(args.state), schema, args.pdf_dir)
        export_submission(schema, submission, args.output,
                          publication_ids=[row["publication_id"] for row in submission.tables["publications"]])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
