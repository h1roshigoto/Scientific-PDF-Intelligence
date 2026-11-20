"""Extract and verify the two participant D001 table cells into SPI state."""

import argparse
import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pymupdf
from pydantic import ValidationError

from .config import load_project_env
from .gigachat import Completion, GigaChatClient, GigaChatError, is_ultra
from .gigachat_evidence import (
    RAG, SOLAR, EvidenceError, Extraction, Lookup, Source, make_prompt, prepare_source, verify_extraction,
)
from .models import Domain, Evidence, ExtractionMetadata, Metric, ResultRecord, Value
from .pdf import import_pdf
from .storage import Store


SCHEMA_VERSION = "table-cell-v1"
METHOD = "pymupdf_gigachat_json_schema"
CACHE_FILE = "gigachat_verified.json"


@dataclass(frozen=True)
class Profile:
    domain: Domain
    name: str
    page: int
    lookup: Lookup

    @property
    def result_id(self) -> str:
        return f"{self.domain}:D001:{self.lookup.metric.lower()}"


PROFILES = (
    Profile("solar", "solar-d001-table1-pristine-voc-v1", 5, SOLAR),
    Profile("rag", "rag-d001-table1-ra-cot-hotpotqa-em-v1", 6, RAG),
)


def _parse(completion: Completion) -> Extraction:
    if not is_ultra(completion.model_id):
        raise EvidenceError("unexpected_model")
    return Extraction.model_validate_json(completion.content)


def _context(profile: Profile, source: Source, content: bytes) -> dict:
    lookup = profile.lookup
    if profile.domain == "solar":
        context = {"device_label": lookup.subject}
        # D001 describes the Table 1 measurement in the preceding discussion on PDF page 3.
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            text = " ".join(pdf[2].get_text().split())
        paragraph = text.partition("We then measured the J–V characteristics")[2].split("The addition", 1)[0]
        if "Figure 5B and Table 1, the pristine device" in paragraph:
            context["measurement_type"] = "J-V"
        return context
    return {
        "rag_variant": lookup.subject, "generator_model": lookup.model,
        "dataset_name": lookup.dataset,
        "task_type": "multi-hop QA" if any("multi-hop QA datasets" in b.text for b in source.blocks) else None,
    }


def process_pdf(store: Store, path: Path, profile: Profile, complete: Callable[..., Completion]) -> dict:
    outcome = {"domain": profile.domain, "status": "needs_review", "cache_hit": False}
    try:
        if path.stem != "D001" or path.suffix.lower() != ".pdf":
            raise EvidenceError("expected_d001_pdf")
        content = path.read_bytes()
        document = import_pdf(store, path, profile.domain, content=content)
        source = prepare_source(path, profile.page, profile.lookup, content=content)
        context = _context(profile, source, content)
    except EvidenceError as exc:
        return {**outcome, "error": str(exc)}
    except FileNotFoundError:
        return {**outcome, "error": "pdf_missing"}
    except (OSError, ValueError, pymupdf.FileDataError):
        return {**outcome, "error": "pdf_or_state_unavailable"}

    digest = document["sha256"]
    cache_key = f"{digest}:{SCHEMA_VERSION}:{profile.name}"
    for entry in store.read(CACHE_FILE):
        if entry["key"] != cache_key:
            continue
        try:
            completion = Completion.model_validate(entry["completion"])
            extracted = _parse(completion)
            verify_extraction(extracted, source, profile.lookup)
        except (ValidationError, EvidenceError):
            break
        outcome["cache_hit"] = True
        break
    if not outcome["cache_hit"]:
        try:
            completion = complete(make_prompt(profile.lookup, source), schema=Extraction.model_json_schema())
            extracted = _parse(completion)
        except GigaChatError as exc:
            return {**outcome, "stage": exc.stage, "error": exc.code,
                    "http_status": exc.status, "model_id": exc.model_id}
        except ValidationError:
            return {**outcome, "model_id": completion.model_id, "error": "invalid_schema"}
        except EvidenceError as exc:
            return {**outcome, "error": str(exc)}

    error = None
    try:
        value, unit = verify_extraction(extracted, source, profile.lookup)
        normalized = Value(value=str(value), unit=unit or None)
    except EvidenceError as exc:
        error = str(exc)
        normalized = Value()

    lookup = profile.lookup
    result = ResultRecord(
        result_id=profile.result_id, document_id=document["document_id"], domain=profile.domain,
        metric=Metric(name=lookup.metric), raw=Value(value=extracted.raw_value, unit=extracted.raw_unit or None),
        normalized=normalized,
        source=Evidence(
            page=source.page, table="Table 1", row=lookup.subject,
            column=" / ".join(filter(None, (lookup.model, lookup.dataset, lookup.metric))),
            origin="primary", fragment=source.target.text if error is None else None,
        ),
        context=context, extraction_status="verified" if error is None else "needs_review", note=error,
        extraction_metadata=ExtractionMetadata(
            model_id=completion.model_id, method=METHOD, pdf_sha256=digest, schema_version=SCHEMA_VERSION,
            profile=profile.name, source_block_id=source.target.block_id if error is None else None,
        ),
    )
    if error is None and not outcome["cache_hit"]:
        store.upsert(CACHE_FILE, {"key": cache_key, "completion": completion.model_dump()}, "key")
    if not store.save_extraction(result):
        return {**outcome, "error": "document_changed"}
    outcome.update(status=result.extraction_status, result_id=result.result_id, model_id=completion.model_id)
    if error is not None:
        outcome["error"] = error
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solar-pdf", type=Path, default=Path("data/solar/dev/D001.pdf"))
    parser.add_argument("--rag-pdf", type=Path, default=Path("data/rag/dev/D001.pdf"))
    parser.add_argument("--state", default="state")
    args = parser.parse_args(argv)
    load_project_env()
    store = Store(args.state)
    with ExitStack() as stack:
        @cache
        def client() -> GigaChatClient:
            return stack.enter_context(GigaChatClient())

        def complete(prompt: str, *, schema: dict) -> Completion:
            return client().complete_json(prompt, schema=schema)

        outcomes = [process_pdf(store, path, profile, complete)
                    for path, profile in zip((args.solar_pdf, args.rag_pdf), PROFILES)]
    print(json.dumps(outcomes, ensure_ascii=True, indent=2))
    return int(any(item["status"] != "verified" for item in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
