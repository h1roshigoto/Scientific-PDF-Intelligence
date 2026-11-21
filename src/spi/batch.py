"""Checkpointed extraction over the same PDF import and storage as the legacy CLI."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from pydantic import ValidationError

from .batch_checks import quality_gate, validate_results
from .batch_evidence import (
    MODEL_PLAN_VERSION, PROFILES, SCHEMA_VERSIONS, SUPPLEMENT, Reply, TableValueMismatch, make_prompt, model_fragments,
    link_table_context, offline_proposals, relevant, unresolved_table_cells, verify,
)
from .batch_pdf import LAYOUT_VERSION, page_fragments
from .gigachat import (
    DEFAULT_COMPLETION_MAX_TOKENS, Completion, GigaChatClient, GigaChatError, completion_diagnostics, is_ultra,
)
from .gigachat_evidence import EvidenceError
from .models import PublicationScope, ResultRecord
from .pdf import parse_pdf
from .storage import Store


def discover(root, publication_ids=(), limit=None):
    root = Path(root)
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf") if root.is_dir() else [root]
    seen = {}
    for path in paths:
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected an existing PDF: {path}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", path.stem):
            raise ValueError(f"Invalid publication ID: {path.stem}")
        if path.stem.lower() in seen:
            raise ValueError(f"Duplicate publication ID in one track: {path.stem}")
        seen[path.stem.lower()] = path
    if publication_ids and not set(publication_ids) <= {p.stem for p in paths}:
        raise ValueError("At least one selected PDF is missing")
    paths = [p.resolve() for p in paths if not publication_ids or p.stem in publication_ids]
    if not paths:
        raise ValueError("No PDF files found")
    return paths[:limit]


def _scope(store, record):
    digest = record.get("sha256")
    reason = "Manual review against track criteria is required"
    if not digest:
        return {"status": "needs_review", "reason": reason}
    scope = store.publication_scopes().get((record["document_id"], digest), PublicationScope(
        document_id=record["document_id"], pdf_sha256=digest, status="needs_review", reason=reason,
    ))
    return scope.model_dump(mode="json")


def report(store, domain, selected=None, *, requests=None):
    records = [r for r in store.batch_documents(domain) if selected is None or r["document_id"] in selected]
    documents = []
    statuses, page_statuses, fields = Counter(), Counter(), Counter()
    result_statuses, review_reasons = Counter(), Counter()
    for record in records:
        item = dict(record, scope=_scope(store, record))
        statuses[item["status"]] += 1
        page_statuses.update(p["read_status"] for p in item.get("pages", ()))
        fragments = item.get("fragments", {}).values()
        results = [r for fragment in fragments for r in fragment.get("results", [])]
        for fragment in fragments:
            review_reasons.update(fragment.get("issues", ()))
        review_reasons.update(issue["reason"] for issue in item.get("layout_issues", ()))
        result_statuses.update(r["extraction_status"] for r in results)
        fields.update(k for r in results if r["extraction_status"] == "verified" for k in r["context"]
                      if k not in {"evidence_blocks", "header_path", "unit_status", "origin_evidence", "proposed_facts",
                                   "context_evidence"})
        documents.append(item)
    return {
        "domain": domain, "coverage": {"documents": dict(statuses), "pages": dict(page_statuses),
                                       "results": dict(result_statuses), "verified_context": dict(fields),
                                       "review_reasons": dict(review_reasons)},
        "quality_gate": quality_gate({d["document_id"]: d["sha256"] for d in documents
                                      if d["scope"]["status"] == "in_scope"}, store.results(domain), documents),
        "scope_review_queue": [r["document_id"] for r in documents if r["scope"]["status"] == "needs_review"],
        "requests_this_run": requests or [],
        "documents": documents,
    }


def preview(paths, domain, store):
    current = {d["document_id"]: d for d in store.batch_documents(domain)} if store else {}
    docs = []
    for path in paths:
        document_id = f"{domain}:{path.stem}"
        try:
            digest = sha256(path.read_bytes()).hexdigest()
            old = current.get(document_id, {})
            status = old.get("status", "pending") if old.get("sha256") == digest else "pending"
            item = {"document_id": document_id, "path": str(path), "sha256": digest, "status": status}
        except OSError as exc:
            item = {"document_id": document_id, "path": str(path), "status": "failed", "error": str(exc)}
        item["scope"] = _scope(store, item) if store else {"status": "needs_review"}
        docs.append(item)
    return {"domain": domain, "dry_run": True, "documents": docs}


class Generation:
    def __init__(self, complete, max_requests=None, completion_max_tokens=DEFAULT_COMPLETION_MAX_TOKENS,
                 retry_invalid_completion=False):
        self.complete = complete
        self.limit = max_requests
        self.completion_max_tokens = completion_max_tokens
        self.retry_invalid_completion = retry_invalid_completion
        self.calls = 0
        self.events = []

    def extract(self, store, record, fragment):
        domain = record["domain"]
        event = {"document_id": record["document_id"], "page": fragment.page,
                 "fragment": fragment.id, "cache_hit": False, "valid_json": False,
                 "verified_quotes": 0, "verified_context_links": 0}
        self.events.append(event)
        started = perf_counter()
        completion = None
        attempted = False
        cache_key = sha256(f"{record['revision']}:{MODEL_PLAN_VERSION}:{fragment.id}".encode()).hexdigest()
        cache_file = Path("batch-cache") / f"{cache_key}.json"
        try:
            if self.complete is None:
                proposals, model = offline_proposals(fragment, domain), "local"
            else:
                cached = store.read(cache_file)
                if cached:
                    completion = Completion.model_validate(cached)
                    event["cache_hit"] = True
                elif self.limit is not None and self.calls >= self.limit:
                    event["error"] = "request_budget_exhausted"
                    return {"status": "pending", "error": event["error"], "page": fragment.page}
                else:
                    self.calls += 1
                    attempted = True
                    completion = self.complete(make_prompt(fragment, domain), schema=Reply.model_json_schema())
                event.update(completion_diagnostics(completion))
                if not is_ultra(completion.model_id):
                    raise GigaChatError("completion", "unexpected_model", model_id=completion.model_id,
                                        finish_reason=completion.finish_reason, usage=completion.usage,
                                        max_tokens=completion.max_tokens, retries=completion.retries)
                model = completion.model_id
                proposals = Reply.model_validate_json(completion.content).results
                event["valid_json"] = True
        except GigaChatError as exc:
            event.update(completion_diagnostics(exc), error=exc.code, stage=exc.stage, http_status=exc.status)
            return {"status": "pending", "error": f"{exc.stage}:{exc.code}", "page": fragment.page,
                    "completion": event}
        except ValidationError:
            event["error"] = "invalid_schema"
            if self.retry_invalid_completion:
                return {"status": "pending", "error": "completion:invalid_schema", "page": fragment.page}
            return {"status": "review", "issues": ["invalid_schema"], "page": fragment.page}
        finally:
            if attempted and event.get("requested_max_tokens") is None:
                event["requested_max_tokens"] = self.completion_max_tokens
            event["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)

        results, issues, review_details, rejected = {}, [], [], []
        for proposal in proposals:
            try:
                result, errors = verify(proposal, fragment, domain, record["document_id"], record["sha256"], model)
                issues.extend(errors)
                results[result.result_id] = result.model_dump(mode="json")
                event["verified_quotes"] += 1
                event["verified_context_links"] += int(result.extraction_status == "verified")
            except TableValueMismatch as exc:
                issues.append(str(exc))
                review_details.append({"code": str(exc), "source_block_id": proposal.block, "fields": exc.fields})
                rejected.append({"code": str(exc), "proposal": proposal.model_dump(mode="json")})
            except EvidenceError as exc:
                issues.append(str(exc))
                rejected.append({"code": str(exc), "proposal": proposal.model_dump(mode="json")})
        if self.complete is None or not proposals:
            unresolved = list(unresolved_table_cells(fragment, domain, record["sha256"]))
            issues.extend(detail["code"] for detail in unresolved)
            review_details.extend(unresolved)
            if self.complete is not None and fragment.cells and not unresolved:
                issues.append("no_supported_proposal")
        if completion is not None and not event["cache_hit"] and (not issues or not proposals):
            store.write(cache_file, completion.model_dump(mode="json"))
        outcome = {"status": "review" if issues else "done", "page": fragment.page,
                   "location": fragment.location, "results": list(results.values()),
                   "issues": list(dict.fromkeys(issues)),
                   "review_source": dict(fragment.payload(), pdf_sha256=record["sha256"]) if issues else None}
        if review_details:
            outcome["review_details"] = review_details
        if rejected:
            outcome["rejected_proposals"] = rejected
        return outcome


def _checkpoint(store, record):
    store.save_batch_document(record)


def _plan_model_fragments(fragments, saved):
    planned = []
    for fragment in fragments:
        units = model_fragments(fragment)
        planned.extend(units)
        old = saved.get(fragment.id, {})
        # A previous answer could cover only some of the cells in a grouped fragment.
        # Keep its validated records and let any uncovered cells be scheduled individually.
        for unit in units:
            if not unit.cells or unit.id in saved:
                continue
            cell_id = unit.cells[0].value.block_id
            results = [r for r in old.get("results", ())
                       if (r.get("extraction_metadata") or {}).get("source_block_id") == cell_id]
            if results:
                review = any(r["extraction_status"] != "verified" for r in results)
                issues = [r["note"] for r in results if r.get("note")]
                saved[unit.id] = {
                    "status": "review" if review else "done", "page": unit.page,
                    "location": unit.location, "results": results, "issues": issues,
                    "review_source": unit.payload() if review else None,
                }
    return planned


def _process(store, path, domain, generation, previous, *, prioritize_cell=None, content=None):
    pid = f"{domain}:{path.stem}"
    record = {"document_id": pid, "path": str(path), "domain": domain,
              "status": "running", "sha256": None, "page_count": None, "pages": [],
              "pages_needing_ocr": [], "errors": [], "fragments": {}}
    try:
        if content is None:
            content = path.read_bytes()
        digest = sha256(content).hexdigest()
        mode = "local" if generation.complete is None else "gigachat"
        schema_version = SCHEMA_VERSIONS[domain]
        revision = f"{digest}|{LAYOUT_VERSION}|{PROFILES[domain]}|{schema_version}|{mode}"
        if previous and previous.get("revision") == revision:
            record = {**previous, "path": str(path)}
            plan_is_current = mode == "local" or record.get("model_plan_version") == MODEL_PLAN_VERSION
            if not prioritize_cell and plan_is_current and record["status"] in {"processed", "partial"} and not any(
                unit["status"] == "pending" for unit in record["fragments"].values()
            ) and not record.get("retry_parse") and not record.get("pending_units") and (
                record.get("scope_status_at_run") == _scope(store, record)["status"]
            ):
                # Preserve the imported identity and file path when a checkpoint is moved.
                imported = next((d for d in store.documents() if d["document_id"] == pid), None)
                if imported and imported.get("sha256") == digest and imported["file_path"] == str(path):
                    return
        record.update(sha256=digest, revision=revision, status="running", parser_version=LAYOUT_VERSION,
                      profile=PROFILES[domain], schema_version=schema_version, mode=mode, errors=[])
        _checkpoint(store, record)
        fragments, layout_issues = [], []
        previous_captions = {}

        def inspect_page(page, blocks):
            units, unresolved = page_fragments(page, blocks, lambda text: relevant(text, domain),
                                               previous_captions=previous_captions, row_metrics=domain == "rag")
            fragments.extend(units)
            layout_issues.extend(unresolved)

        metadata, pages = parse_pdf(path, domain, content=content, inspect_page=inspect_page)
        fragments = list(link_table_context(fragments, pages, domain))
        for issue in layout_issues:
            issue["pdf_sha256"] = digest
        store.save_document(metadata, pages)
        record.update(page_count=metadata["page_count"], pages_needing_ocr=metadata["pages_needing_ocr"],
                      pages=[{key: value for key, value in page.items() if key != "blocks"} for page in pages],
                      layout_issues=layout_issues, retry_parse=any(p.get("error") or p.get("analysis_error") for p in pages))
        supplementary = [
            {"page": page["page"], "quote": match[0]} for page in pages for block in page["blocks"]
            if (match := SUPPLEMENT.search(block["text"]))
        ]
        record["supplement"] = {"status": "mentioned_not_provided" if supplementary else "not_detected_in_read_pages",
                                "evidence": list({(s["page"], s["quote"]): s for s in supplementary}.values())}
        if domain == "solar":
            record["measurement_notes"] = [
                {"page": page["page"], "fragment": block["text"][:1250], "bbox": block["bbox"]}
                for page in pages for block in page["blocks"]
                if re.search(r"\b(?:solar simulator|source meter|source.measure unit)\b", block["text"], re.I)
            ]
        decision = _scope(store, record)
        record["scope_status_at_run"] = decision["status"]
        if mode == "gigachat":
            fragments = _plan_model_fragments(fragments, record["fragments"])
            record["model_plan_version"] = MODEL_PLAN_VERSION
        record["planned_fragments"] = len(fragments)
        live = {fragment.id for fragment in fragments}
        record["fragments"] = {key: val for key, val in record["fragments"].items() if key in live}
        if decision["status"] == "out_of_scope":
            record.update(status="processed", extraction_coverage="excluded_after_scope_review", pending_units=0)
            _checkpoint(store, record)
            return
        # Prioritize proven table geometry before optional prose.
        fragments.sort(key=lambda fragment: (not bool(fragment.cells), fragment.page, fragment.id))
        if prioritize_cell:
            matches = [f for f in fragments if f.cells and f.cells[0].value.block_id == prioritize_cell]
            if len(matches) != 1:
                raise ValueError("The requested source block must match exactly one table cell")
            fragments.remove(matches[0])
            fragments.insert(0, matches[0])
        _checkpoint(store, record)
        for fragment in fragments:
            old = record["fragments"].get(fragment.id)
            if old and old["status"] != "pending":
                continue
            if generation.complete is None and not fragment.cells:
                continue
            outcome = generation.extract(store, record, fragment)
            if outcome.get("error") == "request_budget_exhausted":
                break
            record["fragments"][fragment.id] = outcome
            _checkpoint(store, record)
            if outcome.get("error", "").startswith(("config:", "oauth:")):
                break
        results = [ResultRecord.model_validate(r) for outcome in record["fragments"].values()
                   for r in outcome.get("results", [])]
        checks, issues = validate_results(results)
        pending_units = sum((generation.complete is not None or bool(f.cells)) and
                            record["fragments"].get(f.id, {}).get("status", "pending") == "pending" for f in fragments)
        record.update(science_checks=checks, science_issues=[i.model_dump(mode="json") for i in issues],
                      pending_units=pending_units)
        record["errors"] = [
            {"page": f["page"], "reason": f["error"]} for f in record["fragments"].values() if "error" in f
        ]
        record["extraction_coverage"] = (
            "pdf_pages_unprocessed" if record["retry_parse"] or record["pages_needing_ocr"] else
            "model_fragments_pending" if pending_units else
            "present_not_extractable" if not results and layout_issues else
            "partial_table_only" if generation.complete is None else "selected_fragments_reviewed"
        )
        record["status"] = "partial" if (
            pending_units or record["pages_needing_ocr"] or record["retry_parse"] or layout_issues
            or not results or any(f["status"] == "review" for f in record["fragments"].values())
        ) else "processed"
    except (OSError, RuntimeError, ValueError) as exc:
        record.update(status="failed", errors=[{"reason": f"{type(exc).__name__}: {exc}"}])
    _checkpoint(store, record)


def run_batch(store, paths, domain, *, local_only, max_requests=None, complete=None, json_only=False,
              completion_max_tokens=DEFAULT_COMPLETION_MAX_TOKENS, prioritize_cell=None, pdf_content=None,
              retry_invalid_completion=False):
    if prioritize_cell and (local_only or len(paths) != 1):
        raise ValueError("Prioritizing a source cell requires a model batch of exactly one PDF")
    if pdf_content is not None and len(paths) != 1:
        raise ValueError("Supplying PDF bytes requires exactly one PDF")
    with ExitStack() as stack:
        try:
            stack.enter_context(store.batch_lock())
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise ValueError("Another batch is running in this state directory") from exc
            raise
        client = None

        def live(prompt, *, schema):
            nonlocal client
            if client is None:
                options = {"max_completion_retries": 0} if max_requests is not None else {}
                client = stack.enter_context(GigaChatClient(**options))
            return client.complete_json(prompt, schema=None if json_only else schema, max_tokens=completion_max_tokens)

        generation = Generation(None if local_only else complete or live, max_requests, completion_max_tokens,
                                retry_invalid_completion)
        prior = {r["document_id"]: r for r in store.batch_documents(domain)}
        selected = {f"{domain}:{path.stem}" for path in paths}
        # Register every selected PDF before the first expensive operation.
        for path in paths:
            pid = f"{domain}:{path.stem}"
            if pid not in prior:
                _checkpoint(store, {"document_id": pid, "path": str(path), "domain": domain, "status": "pending"})
        for path in paths:
            _process(store, path, domain, generation, prior.get(f"{domain}:{path.stem}"),
                     prioritize_cell=prioritize_cell, content=pdf_content)
        return report(store, domain, selected, requests=generation.events)
