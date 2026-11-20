"""Run the GigaChat Ultra smoke test without touching API or CSV state."""

import argparse
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from .config import load_project_env
from .gigachat import MODEL_ID, GigaChatClient, GigaChatError, completion_diagnostics, is_ultra
from .gigachat_evidence import (
    RAG,
    SOLAR,
    Block,
    EvidenceError,
    Extraction,
    Lookup,
    Source,
    make_prompt,
    normalize_cell,
    prepare_source,
    quote_matches,
    source_payload,
    verify_extraction,
)


@dataclass(frozen=True)
class SmokeCase:
    name: str
    lookup: Lookup
    source: Source
    # Compared locally only after parsing, evidence checks and normalization.
    expected: Decimal | None = None


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _control(index: int) -> SmokeCase:
    subject = f"Control {index}"
    caption = Block(f"control-{index}-caption", (0, 0, 80, 8), "Table 1. Synthetic control")
    header = Block(f"control-{index}-header", (20, 10, 80, 18), "Reading")
    row = Block(f"control-{index}-row", (0, 20, 20, 28), subject)
    cell = Block(f"control-{index}-cell", (20, 20, 80, 28), str(index + 2))
    return SmokeCase(
        f"synthetic_control_{index}",
        Lookup(subject, header.text, ""),
        Source(1, (caption, header, row, cell), cell),
    )


def _complete(client: GigaChatClient, case: SmokeCase, strict: bool) -> dict:
    started = time.monotonic()
    observation = {
        "case": case.name,
        "mode": "strict_json_schema" if strict else "prompted_json",
        "model_id": None,
        "json_parsed": False,
        "schema_valid": False,
        "quote_matches": None,
        "table_cell_verified": False,
        "status": "needs_review",
    }
    try:
        completion = client.complete_json(
            make_prompt(case.lookup, case.source),
            schema=Extraction.model_json_schema() if strict else None,
        )
        observation.update(completion_diagnostics(completion))
        observation["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        data = json.loads(completion.content)
        observation["json_parsed"] = True
        result = Extraction.model_validate(data)
        observation["schema_valid"] = True
        observation["quote_matches"] = quote_matches(result, case.source)
        value, unit = verify_extraction(result, case.source, case.lookup)
        observation.update(
            table_cell_verified=True,
            raw_value=result.raw_value,
            raw_unit=result.raw_unit,
            normalized_value=str(value),
            normalized_unit=unit,
            pdf_page=case.source.page,
            source_block_id=result.source_block_id,
            source_quote=result.source_quote,
        )
        if case.expected is not None and value != case.expected:
            raise EvidenceError("control_value_mismatch")
        observation["status"] = "verified"
    except GigaChatError as exc:
        observation.update(completion_diagnostics(exc), error=exc.code, stage=exc.stage, http_status=exc.status)
    except json.JSONDecodeError:
        observation["error"] = "invalid_json"
    except ValidationError:
        observation["error"] = "invalid_schema"
    except EvidenceError as exc:
        observation["error"] = str(exc)
    observation.setdefault("elapsed_ms", round((time.monotonic() - started) * 1000))
    return observation


def _run_live(report: dict, cases: list[SmokeCase], json_only: bool) -> None:
    strict = not json_only
    if json_only:
        report["strict_schema"] = "skipped_by_option"
    try:
        with GigaChatClient() as client:
            models = client.list_models()
            report["oauth"] = "authorized"
            report["models_endpoint"] = "ok"
            report["ultra_listed"] = any(is_ultra(model) for model in models)
            if not report["ultra_listed"]:
                raise GigaChatError("models", "ultra_not_listed")
            for case in cases:
                observation = _complete(client, case, strict)
                report["requests"].append(observation)
                reason = observation.get("finish_reason")
                error = observation.get("error")
                if observation["model_id"] and reason == "stop" and error not in {"unexpected_model", "empty_response"}:
                    report["ultra_completion_succeeded"] = True
                if strict:
                    if error == "strict_schema_rejected":
                        report["strict_schema"] = "rejected_using_prompted_json"
                        strict = False
                    elif observation["schema_valid"]:
                        report["strict_schema"] = "observed_schema_compliant_response"
                    elif report["strict_schema"] == "not_checked":
                        report["strict_schema"] = "not_confirmed"
                if reason not in {None, "stop"} or observation.get("http_status") in {401, 403, 404} or error in {
                    "unexpected_model", "unexpected_finish_reason", "invalid_choice_count", "tls_error",
                }:
                    break
    except GigaChatError as exc:
        if exc.stage == "models":
            report["oauth"] = "authorized"
            if report["models_endpoint"] != "ok":
                report["models_endpoint"] = "failed"
        elif exc.stage == "oauth":
            report["oauth"] = "failed"
        report["blocker"] = {"stage": exc.stage, "error": exc.code, "http_status": exc.status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solar-pdf", type=Path, default=Path("data/solar/dev/D001.pdf"))
    parser.add_argument("--rag-pdf", type=Path, default=Path("data/rag/dev/D001.pdf"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/gigachat-smoke"))
    parser.add_argument("--prepare-only", action="store_true", help="Extract local fragments without network calls")
    parser.add_argument("--json-only", action="store_true", help="Use prompted JSON with local validation")
    args = parser.parse_args(argv)
    load_project_env()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("spi.gigachat").setLevel(logging.INFO)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "requested_model": MODEL_ID,
        "oauth": "not_checked",
        "models_endpoint": "not_checked",
        "ultra_listed": None,
        "ultra_completion_succeeded": False,
        "strict_schema": "not_checked",
        "sources": {},
        "requests": [],
    }
    cases = [_control(1)]
    for index, (name, path, page, lookup, expected) in enumerate(
        (
            ("solar_d001", args.solar_pdf, 5, SOLAR, Decimal("1.197")),
            ("rag_d001", args.rag_pdf, 6, RAG, Decimal("0.204")),
        ),
        start=2,
    ):
        fragment_path = args.output_dir / f"{name}_source.json"
        try:
            source = prepare_source(path, page, lookup)
        except EvidenceError as exc:
            fragment_path.unlink(missing_ok=True)
            report["sources"][name] = {"status": "needs_review", "pdf_page": page, "error": str(exc)}
            cases.append(_control(index))
            continue
        _write_json(fragment_path, source_payload(source))
        raw_value, normalized_value, normalized_unit = normalize_cell(source, lookup)
        report["sources"][name] = {
            "status": "prepared",
            "pdf_page": page,
            "fragment": fragment_path.name,
            "local_pdf_cell": {
                "raw_value": raw_value,
                "raw_unit": lookup.unit,
                "normalized_value": str(normalized_value),
                "normalized_unit": normalized_unit,
                "source_block_id": source.target.block_id,
                "source_quote": source.target.text,
                "matches_control": normalized_value == expected,
            },
        }
        cases.append(SmokeCase(name, lookup, source, expected))

    if not args.prepare_only:
        _run_live(report, cases, args.json_only)
    report_path = args.output_dir / "report.json"
    _write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.prepare_only:
        return int(any(source["status"] != "prepared" for source in report["sources"].values()))
    verified = {item["case"] for item in report["requests"] if item["status"] == "verified"}
    return int(not {"solar_d001", "rag_d001"} <= verified)


if __name__ == "__main__":
    raise SystemExit(main())
