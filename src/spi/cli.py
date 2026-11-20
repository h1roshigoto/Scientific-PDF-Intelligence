"""Import PDFs without inventing scientific metric values."""
import argparse
import json
from pathlib import Path
from .pdf import import_pdf
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--domain", choices=["solar", "rag"], required=True)
    parser.add_argument("--state", default="state")
    args = parser.parse_args()
    paths = sorted(args.path.rglob("*.pdf")) if args.path.is_dir() else [args.path]
    if not paths:
        parser.error("No PDF files found")
    for path in paths:
        try:
            print(json.dumps(import_pdf(Store(args.state), path, args.domain), ensure_ascii=False))
        except Exception as error:
            print(json.dumps({"path": str(path), "error": str(error)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
