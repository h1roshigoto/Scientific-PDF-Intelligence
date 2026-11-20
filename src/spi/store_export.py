"""Export SPI records to the seven participant CSV files for a track."""

import argparse
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from .csv_export import PDF_ID, Submission, SubmissionError, export_submission
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


def project_submission(
    store: Store, schema: SubmissionSchema, pdf_dir: Path, *, publication_ids: Sequence[str],
) -> Submission:
    tables = {name: [] for name in schema.tables}
    selected = set(publication_ids)
    if not selected or len(selected) != len(publication_ids) or any(not PDF_ID.fullmatch(pid) for pid in selected):
        raise SubmissionError("Select one or more distinct PDF stems with --publication-id")
    pdfs = {}
    for path in pdf_dir.iterdir():
        if path.stem in selected and path.suffix.lower() == ".pdf" and path.is_file():
            if path.stem in pdfs:
                raise SubmissionError(f"Multiple PDFs match {path.stem} in {pdf_dir}")
            pdfs[path.stem] = path
    documents = {d["publication_id"]: d for d in store.documents() if d["domain"] == schema.domain}
    scopes = store.publication_scopes()
    included = {}
    for pid in publication_ids:
        document_id = f"{schema.domain}:{pid}"
        pdf = pdfs.get(pid)
        if pdf is None:
            raise SubmissionError(f"Selected PDF {pid}.pdf is missing in {pdf_dir}")
        doc = documents.get(pid)
        if doc is None:
            raise SubmissionError(f"Selected PDF {document_id} has not been imported into SPI")
        digest = sha256(pdf.read_bytes()).hexdigest()
        if doc.get("sha256") != digest:
            raise SubmissionError(f"Selected PDF {document_id} does not match the imported SHA-256")
        decision = scopes.get((document_id, digest))
        if decision is None or decision.status == "needs_review":
            raise SubmissionError(
                f"{document_id}: publication scope needs_review. Review the PDF against the track criteria "
                "and record the decision with python -m spi.publication_scope before exporting."
            )
        in_scope = decision.status == "in_scope"
        tables["publications"].append({"publication_id": pid, "in_scope": in_scope,
                                       "exclusion_reason": None if in_scope else decision.reason})
        if in_scope:
            included[document_id] = doc

    for result in store.results(schema.domain):
        doc = included.get(result.document_id)
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
    parser.add_argument("--publication-id", action="append", required=True,
                        help="PDF stem to export. Repeat for each selected publication.")
    parser.add_argument("--state", default="state")
    parser.add_argument("--template-dir", type=Path)
    args = parser.parse_args()
    try:
        schema = load_schema(args.domain, args.template_dir)
        submission = project_submission(Store(args.state), schema, args.pdf_dir,
                                        publication_ids=args.publication_id)
        export_submission(schema, submission, args.output, publication_ids=args.publication_id)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
