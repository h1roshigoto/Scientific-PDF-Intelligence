"""Create a local, read-only bibliography review sheet from registered PDF originals.

Values from embedded PDF metadata are candidate hints, not verified bibliographic
facts. This module does not change the Store, source PDFs, or official CSVs.
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import pymupdf

from .storage import Store


FIELDS = (
    "document_id", "domain", "publication_id", "pdf_sha256", "page_count",
    "pdf_filename", "title_in_store", "title_candidate", "title_candidate_source",
    "year_in_store", "year_candidate", "year_candidate_source",
    "DOI_in_store", "DOI_candidate", "DOI_candidate_source",
    "authors_in_store", "authors_candidate", "authors_candidate_source",
    "review_status", "confirmed_title", "confirmed_year", "confirmed_DOI",
    "confirmed_authors", "reviewer", "source_pages", "source_reference",
    "review_note",
)


def _value(value):
    if value is None:
        return ""
    return str(value).strip()


def _safe_cell(value):
    """Prevent embedded PDF metadata from becoming a spreadsheet formula."""
    text = _value(value)
    if text.lstrip("\ufeff \t\r\n").startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + text
    return text


def _hint(value):
    text = _safe_cell(value)
    return text, "embedded_pdf_metadata" if text else ""


def _pdf_metadata(record):
    key = record["document_id"]
    domain = record["domain"]
    pid = record["publication_id"]
    if domain not in {"solar", "rag"} or key != f"{domain}:{pid}":
        raise ValueError(f"{key}: inconsistent registered identity")
    path = Path(record["file_path"]).resolve(strict=True)
    if path.suffix.lower() != ".pdf" or path.stem != pid:
        raise ValueError(f"{key}: original filename differs from registration")
    content = path.read_bytes()
    from hashlib import sha256
    digest = sha256(content).hexdigest()
    if digest != record["sha256"]:
        raise ValueError(f"{key}: original PDF SHA-256 changed; no review sheet written")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            if pdf.needs_pass or not pdf.is_pdf or pdf.page_count != record["page_count"]:
                raise ValueError(f"{key}: PDF encrypted, invalid, or page count differs from Store")
            embedded = pdf.metadata or {}
    except (RuntimeError, pymupdf.FileDataError) as exc:
        raise ValueError(f"{key}: cannot inspect registered PDF") from exc
    return path, embedded


def build_rows(store: Store, *, domain="all", publication_id=None):
    registered = store.documents()
    if not isinstance(registered, list) or not registered:
        raise ValueError("Registered documents.json is empty or missing")
    keys = [record["document_id"] for record in registered]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate document IDs in Store")
    selected = [
        record for record in registered
        if (domain == "all" or record["domain"] == domain)
        and (publication_id is None or record["publication_id"] == publication_id)
    ]
    if not selected:
        raise ValueError("No registered PDF matches this selection")
    rows, source_folders = [], set()
    for record in sorted(selected, key=lambda item: (item["domain"], item["publication_id"])):
        path, embedded = _pdf_metadata(record)
        source_folders.add(path.parent)
        title, title_source = _hint(embedded.get("title"))
        authors, authors_source = _hint(embedded.get("author"))
        doi, doi_source = _hint(embedded.get("doi"))
        # A PDF creation/modification timestamp is NOT a publication year.
        # Do not infer DOI or year from filenames, keywords or unrelated dates.
        rows.append({
            "document_id": record["document_id"],
            "domain": record["domain"],
            "publication_id": record["publication_id"],
            "pdf_sha256": record["sha256"],
            "page_count": str(record["page_count"]),
            "pdf_filename": path.name,
            "title_in_store": _safe_cell(record.get("title")),
            "title_candidate": title,
            "title_candidate_source": title_source,
            "year_in_store": _safe_cell(record.get("year")),
            "year_candidate": "",
            "year_candidate_source": "",
            "DOI_in_store": _safe_cell(record.get("DOI") or record.get("doi")),
            "DOI_candidate": doi,
            "DOI_candidate_source": doi_source,
            "authors_in_store": _safe_cell(record.get("authors") or record.get("author")),
            "authors_candidate": authors,
            "authors_candidate_source": authors_source,
            "review_status": "pending",
            "confirmed_title": "",
            "confirmed_year": "",
            "confirmed_DOI": "",
            "confirmed_authors": "",
            "reviewer": "",
            "source_pages": "",
            "source_reference": "",
            "review_note": "",
        })
    return rows, source_folders


def write_review_sheet(store: Store, destination: Path, *, domain="all", publication_id=None):
    destination = Path(destination).resolve()
    root = store.root.resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Review sheet cannot be written inside Store")
    if not destination.parent.is_dir():
        raise ValueError("Review sheet parent directory must already exist")
    rows, source_folders = build_rows(store, domain=domain, publication_id=publication_id)
    if any(destination == folder or folder in destination.parents for folder in source_folders):
        raise ValueError("Review sheet cannot be written inside the original PDF folder")
    # x mode makes pre-existing review files untouchable even under a race.
    created = False
    try:
        with destination.open("x", encoding="utf-8-sig", newline="") as file:
            created = True
            writer = csv.DictWriter(file, fieldnames=FIELDS, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("outputs/p0-21-test-local/state"))
    parser.add_argument("--output", type=Path, required=True, help="New, local CSV file outside Store/PDF directories")
    parser.add_argument("--domain", choices=("all", "solar", "rag"), default="all")
    parser.add_argument("--publication-id", help="Optional PDF stem, e.g. P001")
    args = parser.parse_args(argv)
    if not (args.state / "documents.json").is_file():
        parser.error(f"Store registration missing: {args.state / 'documents.json'}")
    try:
        count = write_review_sheet(
            Store(args.state), args.output, domain=args.domain, publication_id=args.publication_id,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"REVIEW REQUIRED: {count} registered PDF(s); suggestions are not approved metadata")
    print(f"Local review CSV: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
