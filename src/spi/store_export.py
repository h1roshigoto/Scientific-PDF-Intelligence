"""Export SPI records to the seven participant CSV files for a track."""

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from .csv_export import PDF_ID, Submission, SubmissionError, export_submission
from .csv_schema import SubmissionSchema, load_schema
from .models import ResultRecord, ValidationIssue
from .storage import Store


def _related_rows(result: ResultRecord, tables: dict, publication_id: str, schema: SubmissionSchema) -> dict:
    prefix = dict(publication_id=publication_id)
    context = result.context

    def fields(table):
        return {col: context[col] for col in schema.tables[table].columns
                if col in context and not col.endswith("_id")}

    if result.domain == "solar":
        device_id = f"{result.result_id}:device"
        measurement_id = f"{result.result_id}:measurement"
        material_id = None
        if context.get("material_name_raw"):
            material_id = f"{result.result_id}:material"
            tables["materials"].append(dict(fields("materials"), **prefix, material_id=material_id))
        tables["devices"].append(dict(fields("devices"), **prefix, device_id=device_id, absorber_material_id=material_id))
        tables["measurements"].append(dict(fields("measurements"), **prefix, measurement_id=measurement_id,
                                           device_id=device_id))
        for order, layer in enumerate(context.get("layers", []), 1):
            row = {col: layer[col] for col in schema.tables["layers"].columns
                   if col in layer and not col.endswith("_id")}
            tables["layers"].append(dict(row, **prefix, layer_id=f"{device_id}:layer:{order}", device_id=device_id))
        return {"device_id": device_id, "measurement_id": measurement_id, "material_id": material_id}

    config_id = f"{result.result_id}:config"
    dataset_id = f"{result.result_id}:dataset"
    task_id = f"{result.result_id}:task"
    experiment_id = f"{result.result_id}:experiment"
    tables["configurations"].append(dict(fields("configurations"), **prefix, config_id=config_id))
    tables["datasets"].append(dict(fields("datasets"), **prefix, dataset_id=dataset_id))
    tables["tasks"].append(dict(fields("tasks"), **prefix, task_id=task_id))
    experiment = fields("experiments")
    experiment.setdefault("note", f"{result.source.table}, PDF page {result.source.page}")
    tables["experiments"].append(dict(experiment, **prefix, experiment_id=experiment_id))
    return {"config_id": config_id, "dataset_id": dataset_id, "task_id": task_id, "experiment_id": experiment_id}


def _issue_rows(issue: ValidationIssue, exported_results, included):
    related = defaultdict(list)
    for result_id in dict.fromkeys(issue.result_ids):
        if result_id in exported_results:
            related[exported_results[result_id]].append(result_id)

    for document_id, result_ids in related.items():
        document = included[document_id]
        refs = []
        for ref in issue.evidence_refs:
            if ref.get("document_id") != document_id and not (
                not ref.get("document_id") and ref.get("result_id") in result_ids
            ):
                continue
            evidence = dict(ref)
            if evidence.get("result_id") not in result_ids:
                evidence.pop("result_id", None)
            refs.append(evidence)
        # The official CSV has one page and quote pair. Store the complete references in the description.
        primary = next((ref for ref in refs
                        if type(ref.get("page")) is int and 1 <= ref["page"] <= document["page_count"]
                        and isinstance(ref.get("fragment"), str) and ref["fragment"].strip()), {})
        description = [issue.reason]
        if issue.observed is not None:
            description.append(f"Observed value: {issue.observed}")
        if issue.expected is not None:
            description.append(f"Comparison value: {issue.expected}")
        if refs:
            description.extend(("Evidence references (JSON)", json.dumps(refs, ensure_ascii=False)))
        yield {
            "publication_id": document["publication_id"], "issue_id": issue.issue_id,
            "record_ids": ";".join(result_ids), "check_name": issue.rule_id,
            "description": "\n".join(description), "source_page": primary.get("page"),
            "source_fragment": primary.get("fragment"),
        }


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

    exported_results = {}
    for result in store.results(schema.domain):
        doc = included.get(result.document_id)
        metadata = result.extraction_metadata
        if (doc is None or result.extraction_status != "verified" or metadata is None
                or metadata.pdf_sha256 != doc["sha256"]):
            continue
        pid = doc["publication_id"]
        exported_results[result.result_id] = result.document_id
        source = result.source
        notes = [result.note, result.context.get("note")]
        raw_scan = result.context.get("raw_scan_direction")
        if raw_scan:
            notes.append(f"Published scan direction: {raw_scan}")
        if schema.domain == "rag" and source.column:
            notes.append(f"Table header path: {source.column}")
        if result.context.get("metric_definition"):
            notes.append(f"Metric definition: {result.context['metric_definition']}")
        tables["results"].append({
            **_related_rows(result, tables, pid, schema),
            "publication_id": pid, "result_id": result.result_id,
            "metric_name": result.metric.name, "metric_value": result.normalized.value or result.raw.value,
            "metric_unit": result.normalized.unit or result.raw.unit, "raw_value": result.raw.value, "raw_unit": result.raw.unit,
            "normalized_value": result.normalized.value, "normalized_unit": result.normalized.unit,
            "result_origin": source.origin, "cited_publication_id": source.cited_publication_id,
            "source_page": source.page, "source_section": source.section, "source_table": source.table,
            "source_figure": source.figure, "source_fragment": source.fragment, "extraction_status": "extracted",
            "note": " | ".join(filter(None, notes)) or None,
            **({"metric_k": result.metric.k, "metric_variant": result.metric.variant} if schema.domain == "rag"
               else {"aggregation": result.context.get("aggregation")}),
        })
    for issue in store.issues(schema.domain):
        tables["issues"].extend(_issue_rows(issue, exported_results, included))
    return Submission(schema.domain, tables)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", choices=("solar", "rag"))
    parser.add_argument("output", type=Path, help="New submission directory")
    parser.add_argument("--pdf-dir", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--publication-id", action="append", help="PDF stem to export. Repeat for each selected publication.")
    selection.add_argument("--all-pdfs", action="store_true", help="Explicitly select all PDFs in --pdf-dir and check every scope")
    parser.add_argument("--state", default="state")
    parser.add_argument("--template-dir", type=Path)
    args = parser.parse_args()
    try:
        schema = load_schema(args.domain, args.template_dir)
        ids = args.publication_id if not args.all_pdfs else sorted(
            path.stem for path in args.pdf_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
        )
        submission = project_submission(Store(args.state), schema, args.pdf_dir,
                                        publication_ids=ids)
        export_submission(schema, submission, args.output, publication_ids=ids)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
