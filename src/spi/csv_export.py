"""Read, validate and write one track's participant submission."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory

from .csv_schema import SubmissionSchema, load_schema
from .models import Domain


class SubmissionError(ValueError):
    """The input does not match the submission contract."""


Cell = str | int | float | Decimal | bool | None
PDF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
INTEGER = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class Submission:
    domain: Domain
    tables: Mapping[str, Iterable[Mapping[str, Cell]]]
    quality_gate: Mapping[str, object] | None = None


def read_submission(schema: SubmissionSchema, directory: Path) -> Submission:
    """Read CSV in schema order, mapping empty cells to None."""
    directory = Path(directory)
    names = {path.stem for path in directory.glob("*.csv")}
    _require_same_names(names, schema.tables, f"{directory} CSV tables")
    tables = {}
    for name, table in schema.tables.items():
        path = directory / f"{name}.csv"
        try:
            with path.open(encoding="utf-8-sig", newline="") as file:
                reader = csv.reader(file, strict=True)
                if tuple(next(reader, ())) != table.columns:
                    raise SubmissionError(f"{path}: incorrect headers or column order")
                rows = []
                for line, cells in enumerate(reader, 2):
                    if len(cells) != len(table.columns):
                        raise SubmissionError(f"{path}:{line}: incorrect number of cells")
                    rows.append(dict(zip(table.columns, (cell or None for cell in cells))))
        except (OSError, UnicodeError, csv.Error) as error:
            raise SubmissionError(f"{path}: {error}") from error
        tables[name] = rows
    return Submission(schema.domain, tables)


def validate_submission(
    schema: SubmissionSchema, submission: Submission, *, publication_ids: Iterable[str]
) -> None:
    """Validate the submission against the expected set of PDF stems."""
    _validated_rows(schema, submission, publication_ids)


def export_submission(
    schema: SubmissionSchema,
    submission: Submission,
    destination: Path,
    *,
    publication_ids: Iterable[str],
) -> Path:
    """Write all seven UTF-8 CSVs together into a new directory."""
    tables = _validated_rows(schema, submission, publication_ids)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Submission directory already exists: {destination}")
    with TemporaryDirectory(prefix=".spi-", dir=destination.parent) as temp:
        staging = Path(temp) / "submission"
        staging.mkdir()
        for name, rows in tables.items():
            path = staging / f"{name}.csv"
            # CRLF also makes the CSV writer quote bare CR in source fragments.
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(schema.tables[name].columns)
                writer.writerows(row.values() for row in rows)
        staging.rename(destination)
    return destination


def _validated_rows(schema, submission, publication_ids):
    if submission.domain != schema.domain:
        raise SubmissionError(f"{submission.domain} submission cannot use the {schema.domain} schema")
    if not isinstance(submission.tables, Mapping):
        raise SubmissionError("Submission tables must be an object")
    _require_same_names(submission.tables, schema.tables, "Tables")
    tables = {}
    indexes = {}
    for name, table in schema.tables.items():
        rows = []
        index = {}
        incoming = submission.tables[name]
        if not isinstance(incoming, Iterable) or isinstance(incoming, (str, bytes, Mapping)):
            raise SubmissionError(f"{name}: expected a sequence of rows")
        for line, data in enumerate(incoming, 2):
            location = f"{name}.csv:{line}"
            if not isinstance(data, Mapping):
                raise SubmissionError(f"{location}: expected a row object")
            unexpected = set(data) - set(table.columns)
            if unexpected:
                raise SubmissionError(f"{location}: unexpected fields {sorted(map(str, unexpected))}")
            row = {column: _text(data.get(column), location, column) for column in table.columns}
            for column, kind in table.numeric.items():
                _require_number(row[column], kind, f"{location}: {column}")
            for column, allowed in table.enums.items():
                if row[column] and row[column] not in allowed:
                    raise SubmissionError(f"{location}: invalid {column} {row[column]!r}")
            pid = row["publication_id"]
            if not PDF_ID.fullmatch(pid):
                raise SubmissionError(f"{location}: invalid publication_id {pid!r}")
            key = (pid, row[table.key])
            if not key[1] or key[1] != key[1].strip():
                raise SubmissionError(f"{location}: missing {table.key}")
            if key in index:
                raise SubmissionError(f"{location}: duplicate key {key!r}")
            index[key] = row
            rows.append(row)
        tables[name] = rows
        indexes[name] = index

    publications = {row["publication_id"]: row for row in tables["publications"]}
    expected = list(publication_ids)
    if any(not isinstance(pid, str) or not PDF_ID.fullmatch(pid) for pid in expected):
        raise SubmissionError("Invalid publication ID in the PDF manifest")
    if len(set(expected)) != len(expected):
        raise SubmissionError("Duplicate publication IDs in the PDF manifest")
    _require_same_names(publications, expected, "Publications versus PDF manifest")
    _validate_relations(schema, tables, indexes, publications)
    return tables


def _require_same_names(actual, expected, label):
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise SubmissionError(f"{label}: missing {missing}, unexpected {extra}")


def _text(value, location, column):
    if value is None:
        return ""
    if isinstance(value, bool):
        if column in {"in_scope", "mask_used", "certified"}:
            return str(value).lower()
    elif isinstance(value, (str, int, float, Decimal)):
        if not isinstance(value, (float, Decimal)) or Decimal(str(value)).is_finite():
            return str(value)
    raise SubmissionError(f"{location}: invalid scalar for {column}")


def _require_number(value, kind, location):
    if not value:
        return
    try:
        number = Decimal(value) if NUMBER.fullmatch(value) else None
    except InvalidOperation:
        number = None
    if number is None:
        raise SubmissionError(f"{location} must be a finite number with a decimal point")
    if kind.endswith("int") and not INTEGER.fullmatch(value):
        raise SubmissionError(f"{location} must be an unsigned integer")
    if kind == "positive_int" and number < 1:
        raise SubmissionError(f"{location} must be positive")
    if kind == "nonnegative" and number < 0:
        raise SubmissionError(f"{location} must be nonnegative")


def _reference_ids(value, *, many):
    values = value.split(";") if many else [value]
    return filter(None, map(str.strip, values))


def _validate_relations(schema, tables, indexes, publications):
    for pid, row in publications.items():
        if row["in_scope"] not in {"true", "false"}:
            raise SubmissionError(f"{pid}: in_scope must be true or false")
        if bool(row["exclusion_reason"].strip()) != (row["in_scope"] == "false"):
            raise SubmissionError(f"{pid}: exclusion_reason is required only for excluded PDFs")
    for name, rows in tables.items():
        if name == "publications":
            continue
        table = schema.tables[name]
        for line, row in enumerate(rows, 2):
            location = f"{name}.csv:{line}"
            pid = row["publication_id"]
            if pid not in publications:
                raise SubmissionError(f"{location}: unknown publication {pid!r}")
            if publications[pid]["in_scope"] == "false" and name not in schema.excluded_tables:
                raise SubmissionError(f"{location}: dependent row for excluded publication {pid!r}")
            for reference in table.references:
                for target_id in _reference_ids(row[reference.column], many=reference.many):
                    if (pid, target_id) not in indexes[reference.target]:
                        raise SubmissionError(
                            f"{location}: {reference.column}={target_id!r} does not exist "
                            f"in {reference.target} for {pid}"
                        )
            if row.get("source_fragment", "").strip() and not row.get("source_page"):
                raise SubmissionError(f"{location}: source_fragment requires source_page")
            if name == "results":
                _validate_result(schema, row, location)
                if schema.domain == "solar":
                    _validate_solar_measurement_device(row, indexes["measurements"], location)
    _validate_duplicate_chains(indexes["results"])


def _validate_solar_measurement_device(row, measurements, location):
    device_id = row["device_id"].strip()
    measurement_id = row["measurement_id"].strip()
    if not device_id or not measurement_id:
        return
    measurement = measurements[(row["publication_id"], measurement_id)]
    devices = set(_reference_ids(measurement["device_id"], many=True))
    if devices and device_id not in devices:
        raise SubmissionError(
            f"{location}: device_id={device_id!r} is not listed for measurement_id={measurement_id!r}"
        )


def _validate_duplicate_chains(index):
    visited = set()
    for key in index:
        chain = set()
        while key in index and key not in visited:
            if key in chain:
                raise SubmissionError(f"results: duplicate_of cycle for {key!r}")
            chain.add(key)
            target = index[key].get("duplicate_of", "").strip()
            if not target:
                break
            key = (key[0], target)
        visited.update(chain)


def _validate_result(schema, row, location):
    for column in ("metric_name", "result_origin", "extraction_status"):
        if not row[column].strip():
            raise SubmissionError(f"{location}: missing {column}")
    if row.get("result_origin") == "cited" and not row.get("cited_publication_id", "").strip():
        raise SubmissionError(f"{location}: cited result requires cited_publication_id")
    if row.get("result_origin") == "primary" and row.get("cited_publication_id", "").strip():
        raise SubmissionError(f"{location}: primary result has cited_publication_id")
    if row.get("extraction_status") == "extracted":
        if not row.get("raw_value", "").strip() or not row.get("source_page"):
            raise SubmissionError(f"{location}: extracted result requires raw_value and source_page")
        has_fragment = bool(row["source_fragment"].strip())
        if schema.domain == "rag":
            if not has_fragment:
                raise SubmissionError(f"{location}: RAG extracted result requires source_fragment")
        elif not (has_fragment or row["source_table"].strip() or row["source_figure"].strip()):
            raise SubmissionError(f"{location}: extracted result requires a source locator")
    if schema.domain == "rag" and row["normalized_value"]:
        if row["metric_value"] and Decimal(row["metric_value"]) != Decimal(row["normalized_value"]):
            raise SubmissionError(f"{location}: metric_value differs from normalized_value")
        if row["metric_unit"] != row["normalized_unit"]:
            raise SubmissionError(f"{location}: metric_unit differs from normalized_unit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", choices=("solar", "rag"))
    parser.add_argument("input", type=Path, help="JSON object with domain and tables")
    parser.add_argument("output", type=Path, help="new submission directory")
    parser.add_argument("--pdf-dir", required=True, type=Path, help="directory of source PDFs")
    parser.add_argument("--template-dir", type=Path, help="optional participant template to verify")
    args = parser.parse_args()
    try:
        schema = load_schema(args.domain, args.template_dir)
        if not args.pdf_dir.is_dir():
            raise SubmissionError(f"PDF directory is missing: {args.pdf_dir}")
        ids = [file.stem for file in args.pdf_dir.iterdir() if file.is_file() and file.suffix.lower() == ".pdf"]
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        if not isinstance(payload, dict) or set(payload) != {"domain", "tables"}:
            raise SubmissionError("Input must contain domain and tables")
        export_submission(schema, Submission(**payload), args.output, publication_ids=ids)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
