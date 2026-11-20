"""Import PDFs without inventing scientific metric values."""
import argparse
import json
from pathlib import Path
from .batch import discover, preview, report, run_batch
from .config import load_project_env
from .gigachat import DEFAULT_COMPLETION_MAX_TOKENS, MAX_COMPLETION_MAX_TOKENS
from .pdf import import_pdf
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--domain", choices=["solar", "rag"], required=True)
    parser.add_argument("--state", default="state")
    parser.add_argument("--batch", action="store_true", help="Run or resume document extraction")
    parser.add_argument("--dry-run", action="store_true", help="Preview PDF selection and status without writing state")
    parser.add_argument("--manifest", action="store_true", help="Show saved manifest and scope review queue")
    parser.add_argument("--local-only", action="store_true", help="Verify recoverable table cells without API calls")
    parser.add_argument("--publication-id", action="append", default=[])
    parser.add_argument("--limit", type=int, help="Maximum selected PDFs")
    parser.add_argument("--max-requests", type=int, help="Maximum new model calls in this invocation")
    parser.add_argument("--prioritize-cell", help="Process a table cell by source block ID before other cells")
    parser.add_argument(
        "--completion-max-tokens", type=int,
        help=f"Maximum output tokens per GigaChat batch request (default: {DEFAULT_COMPLETION_MAX_TOKENS})",
    )
    parser.add_argument("--json-only", action="store_true", help="Require JSON locally without provider schema negotiation")
    parser.add_argument("--report", type=Path, help="Write the full batch report to a local JSON file")
    args = parser.parse_args(argv)
    load_project_env()
    if args.manifest and (args.path or args.batch or args.dry_run):
        parser.error("--manifest only reads the saved track and does not take a path")
    if not args.manifest and args.path is None:
        parser.error("Specify a PDF or directory")
    if args.limit is not None and args.limit < 1 or args.max_requests is not None and args.max_requests < 0:
        parser.error("--limit must be positive and --max-requests cannot be negative")
    if args.completion_max_tokens is not None:
        if not 1 <= args.completion_max_tokens <= MAX_COMPLETION_MAX_TOKENS:
            parser.error(f"--completion-max-tokens must be between 1 and {MAX_COMPLETION_MAX_TOKENS}")
        if not args.batch or args.dry_run or args.local_only:
            parser.error("--completion-max-tokens requires a model batch (--batch)")
    if args.prioritize_cell is not None and (not args.batch or args.dry_run or args.local_only
                                              or not args.prioritize_cell.strip()):
        parser.error("--prioritize-cell requires a source block ID and a model batch (--batch)")
    if args.batch or args.dry_run or args.manifest:
        try:
            state = Path(args.state)
            if args.manifest:
                payload = report(Store(state), args.domain) if state.is_dir() else {"documents": []}
            else:
                paths = discover(args.path, args.publication_id, args.limit)
                if args.dry_run:
                    payload = preview(paths, args.domain, Store(state) if state.is_dir() else None)
                else:
                    payload = run_batch(Store(state), paths, args.domain, local_only=args.local_only,
                                        max_requests=args.max_requests, json_only=args.json_only,
                                        prioritize_cell=args.prioritize_cell,
                                        completion_max_tokens=args.completion_max_tokens or DEFAULT_COMPLETION_MAX_TOKENS)
            body = json.dumps(payload, ensure_ascii=True, indent=2)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(body + "\n", encoding="utf-8")
                print(json.dumps({"report": str(args.report), "coverage": payload.get("coverage")}))
            else:
                print(body)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        return int(any(doc["status"] == "failed" for doc in payload["documents"]))
    if args.local_only or args.limit is not None or args.max_requests is not None or args.report or args.publication_id or args.json_only:
        parser.error("Batch options require --batch, --dry-run or --manifest")
    paths = sorted(args.path.rglob("*.pdf")) if args.path.is_dir() else [args.path]
    if not paths:
        parser.error("No PDF files found")
    for path in paths:
        try:
            print(json.dumps(import_pdf(Store(args.state), path, args.domain), ensure_ascii=False))
        except Exception as error:
            print(json.dumps({"path": str(path), "error": str(error)}, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
