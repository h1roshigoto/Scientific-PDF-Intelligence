"""CSV schemas transcribed from the case's participant packages."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .models import Domain


class SchemaError(ValueError):
    """The participant template is missing or differs from the pinned schema."""


@dataclass(frozen=True)
class Reference:
    column: str
    target: str
    many: bool = False


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    key: str
    numeric: Mapping[str, str]
    enums: Mapping[str, tuple[str, ...]]
    references: tuple[Reference, ...]


@dataclass(frozen=True)
class SubmissionSchema:
    domain: Domain
    version: str
    tables: Mapping[str, Table]
    excluded_tables: frozenset[str] = field(default_factory=frozenset)


def _table(columns, *, key=None, numeric=None, enums=None, references=()):
    columns = tuple(columns.split())
    return Table(columns, key or columns[1], MappingProxyType(numeric or {}),
                 MappingProxyType(enums or {}), references)


_BOOLEAN = ("true", "false")
_PUBLICATIONS = _table(
    "publication_id title year DOI authors in_scope exclusion_reason",
    key="publication_id",
    numeric={"year": "positive_int"},
    enums={"in_scope": _BOOLEAN},
)
_ISSUES = _table(
    "publication_id issue_id record_ids check_name description source_page source_fragment",
    numeric={"source_page": "positive_int"},
    references=(Reference("record_ids", "results", many=True),),
)
_RESULT_ENUMS = {
    "result_origin": ("primary", "cited"),
    "extraction_status": ("extracted", "present_not_extractable", "absent"),
}

_SOLAR = SubmissionSchema("solar", "solar-package-2026-11-20", MappingProxyType({
    "publications": _PUBLICATIONS,
    "materials": _table(
        "publication_id material_id material_name_raw formula_normalized material_class dimensionality "
        "additives bandgap_value bandgap_unit bandgap_method note",
        numeric={"bandgap_value": "number"},
        enums={
            "material_class": ("perovskite", "CIGS", "CdTe", "kesterite", "Sb2Se3", "thin-film Si", "other"),
            "dimensionality": ("3D", "2D", "2D-3D"),
            "bandgap_method": ("Tauc", "EQE", "PL", "cited"),
        },
    ),
    "devices": _table(
        "publication_id device_id absorber_material_id device_label device_role device_type architecture "
        "substrate_type active_area area_unit area_definition encapsulation note",
        numeric={"active_area": "number"},
        enums={
            "device_role": ("control", "target", "record"),
            "device_type": ("cell", "minimodule", "module"),
            "architecture": ("n-i-p", "p-i-n", "substrate", "superstrate"),
            "substrate_type": ("rigid", "flexible"),
            "area_definition": ("designated illumination", "aperture", "total"),
        },
        references=(Reference("absorber_material_id", "materials"),),
    ),
    "layers": _table(
        "publication_id layer_id device_id layer_order layer_function layer_material layer_thickness thickness_unit "
        "deposition_method annealing_temperature annealing_time atmosphere note",
        numeric={"layer_order": "positive_int", "layer_thickness": "number"},
        enums={
            "layer_function": ("front_contact", "ETL", "absorber", "passivation", "HTL", "interlayer",
                               "recombination", "back_contact"),
        },
        references=(Reference("device_id", "devices"),),
    ),
    "measurements": _table(
        "publication_id measurement_id device_id measurement_type spectrum light_intensity intensity_unit "
        "scan_direction scan_rate mask_used certified certification_lab temperature stability_protocol "
        "stress_conditions note",
        numeric={"light_intensity": "number"},
        enums={
            "measurement_type": ("J-V", "MPP tracking", "EQE", "stability"),
            "scan_direction": ("reverse", "forward", "average"),
            "stability_protocol": ("ISOS-D", "ISOS-L", "ISOS-T", "other"),
            "mask_used": _BOOLEAN,
            "certified": _BOOLEAN,
        },
        references=(Reference("device_id", "devices", many=True),),
    ),
    "results": _table(
        "publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit "
        "aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit "
        "result_origin cited_publication_id source_page source_section source_table source_figure source_fragment "
        "extraction_status duplicate_of note",
        numeric={"metric_value": "number", "normalized_value": "number", "source_page": "positive_int",
                 "spread_value": "number", "n_devices": "nonnegative_int", "time_point": "number"},
        enums={**_RESULT_ENUMS, "aggregation": ("champion", "mean", "median")},
        references=(Reference("device_id", "devices"), Reference("measurement_id", "measurements"),
                    Reference("material_id", "materials"), Reference("duplicate_of", "results")),
    ),
    "issues": _ISSUES,
}), excluded_tables=frozenset({"issues"}))

_RAG = SubmissionSchema("rag", "rag-viniti-adaptation-1.0", MappingProxyType({
    "publications": _PUBLICATIONS,
    "configurations": _table(
        "publication_id config_id rag_variant retrieval_method retriever_model embedding_model embedding_version "
        "chunking_method chunk_size chunk_overlap chunk_unit index_type vector_store top_k reranker_model "
        "generator_model generator_version prompt_strategy context_strategy note",
        numeric={"chunk_size": "nonnegative", "chunk_overlap": "nonnegative", "top_k": "nonnegative_int"},
    ),
    "datasets": _table(
        "publication_id dataset_id dataset_name dataset_version language data_split scientific_domain note",
    ),
    "tasks": _table("publication_id task_id task_type note"),
    "experiments": _table(
        "publication_id experiment_id evaluation_protocol baseline_name hardware latency_ms tokens_per_query "
        "sample_size note",
        numeric={"latency_ms": "nonnegative", "tokens_per_query": "nonnegative", "sample_size": "nonnegative_int"},
    ),
    "results": _table(
        "publication_id result_id experiment_id config_id dataset_id task_id metric_group metric_name metric_variant "
        "metric_direction metric_k metric_value metric_unit baseline_value aggregation sample_size raw_value raw_unit "
        "normalized_value normalized_unit result_origin cited_publication_id source_page source_section source_table "
        "source_figure source_fragment extraction_status duplicate_of note",
        numeric={"metric_k": "nonnegative_int", "metric_value": "number", "baseline_value": "number",
                 "sample_size": "nonnegative_int", "normalized_value": "number", "source_page": "positive_int"},
        enums=_RESULT_ENUMS,
        references=(Reference("experiment_id", "experiments"), Reference("config_id", "configurations"),
                    Reference("dataset_id", "datasets"), Reference("task_id", "tasks"),
                    Reference("duplicate_of", "results")),
    ),
    "issues": _ISSUES,
}))


def load_schema(domain: Domain, template_dir: Path | None = None) -> SubmissionSchema:
    """Load the pinned schema, optionally verifying the original package or template directory."""
    schemas = {"solar": _SOLAR, "rag": _RAG}
    if domain not in schemas:
        raise SchemaError(f"Unknown submission track: {domain!r}")
    schema = schemas[domain]
    if template_dir is None:
        return schema
    directory = Path(template_dir)
    if (directory / "submission_template").is_dir():
        directory = directory / "submission_template"
    for name, table in schema.tables.items():
        path = directory / f"{name}.csv"
        try:
            with path.open(encoding="utf-8-sig", newline="") as file:
                header = tuple(next(csv.reader(file, strict=True), ()))
        except (OSError, UnicodeError, csv.Error) as error:
            raise SchemaError(f"Cannot read template {path}: {error}") from error
        if header != table.columns:
            raise SchemaError(f"{path}: header or column order differs from {schema.version}")
    schema_path = directory / "schema.json"
    if schema.domain == "rag" and schema_path.exists():
        _check_rag_manifest(schema, schema_path)
    return schema


def _check_rag_manifest(schema, path):
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SchemaError(f"Cannot read template {path}: {error}") from error
    expected = {
        "version": schema.version,
        "official_rag_template": False,
        "tables": {name: list(table.columns) for name, table in schema.tables.items()},
        "keys": {name: table.key for name, table in schema.tables.items()},
        "enums": {f"{name}.{column}": list(values)
                  for name, table in schema.tables.items() for column, values in table.enums.items()},
    }
    if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in expected.items()):
        raise SchemaError(f"{path}: differs from {schema.version}")
