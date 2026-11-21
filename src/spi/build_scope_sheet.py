"""Build the local review CSV from the P0 #21 queue and original PDF front matter."""

import argparse
import csv
from hashlib import file_digest
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import unicodedata

import pymupdf

from .csv_export import PDF_ID


INPUTS = ("human_decision", "human_approved", "reviewer", "source_pages", "comment", "rag_demo_criterion")
DISPUTED = {"rag:P007", "rag:P010"}
IDENTITY = ("domain", "publication_id", "pdf_sha256", "pdf_path", "page_count", "preliminary_status")
FIELDS = (
    "domain", "publication_id", "title", "mechanism_summary", "agent_proposal", "review_pages",
    "scope_basis", "priority", *INPUTS, "pdf_sha256", "pdf_path", "page_count",
    "document_id", "preliminary_status", "title_source", "summary_source",
)
QUEUE_FIELDS = {*IDENTITY, "document_id", "agent_proposal", "reason", "evidence_origin",
                "native_text_pointers", "evidence_pages", "review_start_pages"}

SIGNALS = {
    "solar": r"thin.film|solar cell|photovoltaic|absorber|deposit|fabricat|junction|perovskite|cigs|czts|cdte|efficien",
    "rag": r"retriev|generat|document|corpus|context|language model|knowledge|rank|question|evaluat",
}
METHOD = r"we (?:propose|introduce|present|develop|design|build|investigate|train|fabricate|show)|approach|method|framework|system|using|based on"


def pdf_sha256(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def read_scope_rows(content, source, required, *, exact=False):
    reader = csv.DictReader(StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
    header = reader.fieldnames or []
    fields = set(header)
    required = set(required)
    if len(fields) != len(header) or not required <= fields or (exact and fields != required):
        raise ValueError(f"{source}: unexpected CSV header")
    rows = {}
    for row in reader:
        if None in row or None in row.values():
            raise ValueError(f"{source}: wrong number of fields on line {reader.line_num}")
        key, domain, publication = row["document_id"], row["domain"], row["publication_id"]
        if (domain not in {"solar", "rag"} or not PDF_ID.fullmatch(publication)
                or key != f"{domain}:{publication}" or not re.fullmatch(r"[0-9a-f]{64}", row["pdf_sha256"])):
            raise ValueError(f"{source}: invalid publication identity or SHA-256 on line {reader.line_num}")
        if key in rows:
            raise ValueError(f"{source}: duplicate publication {key}")
        rows[key] = row
    return rows


def read_queue(path):
    return read_scope_rows(path.read_bytes(), path, QUEUE_FIELDS)


def read_approval(path):
    return read_scope_rows(path.read_bytes(), path, FIELDS, exact=True)


def clean(text):
    return " ".join(unicodedata.normalize("NFKC", text.replace("\u00ad", "")).split())


def front_matter(pdf):
    page = pdf[0]
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    title_lines = []
    for block in data["blocks"]:
        for line in block.get("lines", []):
            if line["dir"][0] < .99 or line["bbox"][1] > page.rect.height * .45:
                continue
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            phrase = clean("".join(span["text"] for span in line["spans"]))
            if re.match(r"^(?:article|research article|communication|research paper|open access|contents lists available at|arxiv:|https?://|doi:)(?:\s|$)", phrase, re.I):
                phrase = re.sub(r"^(?:article|research article|communication|research paper)\s+", "", phrase, flags=re.I)
                if re.match(r"^(?:article|research article|communication|research paper|open access|contents lists available at|arxiv:|https?://|doi:)(?:\s|$)", phrase, re.I):
                    continue
            if not re.search(r"[A-Za-z]{2}", phrase):
                continue
            size = max(spans, key=lambda s: len(s["text"]))["size"]
            title_lines.append((line["bbox"][1], size, phrase))
    title_lines.sort()
    groups = []
    for y, size, phrase in title_lines:
        if groups and 0 <= y - groups[-1][0] <= size * 1.75 and abs(size - groups[-1][1]) <= .35:
            groups[-1] = (y, size, groups[-1][2] + " " + phrase)
        else:
            groups.append((y, size, phrase))
    options = [(size, -y, phrase) for y, size, phrase in groups if 12 <= len(phrase) <= 290]
    title = max(options, default=(0, 0, ""))[2]
    metadata_title = clean((pdf.metadata or {}).get("title") or "")
    title_key = re.sub(r"[^a-z0-9]", "", title.lower())
    meta_key = re.sub(r"[^a-z0-9]", "", metadata_title.lower())
    front_key = re.sub(r"[^a-z0-9]", "", clean(page.get_text()).lower())
    if 14 <= len(meta_key) <= 255 and (meta_key in front_key or (len(title_key) >= 8 and title_key in meta_key)):
        title = metadata_title
    text = "\n".join("".join(span["text"] for span in line["spans"])
                     for block in data["blocks"] for line in block.get("lines", []))
    marker = re.search(r"\b(?:a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|summary)\b[\s.:—–-]*", text, re.I)
    abstract = text[marker.end():] if marker else ""
    abstract = re.split(r"(?im)^\s*(?:(?:\d+\.?\s*|I\.\s+)?(?:introduction|background)\b|keywords\b|index terms\b|CCS concepts\b)", abstract)[0]
    return title, clean(abstract)


def abstract_excerpt(domain, abstract, pointers):
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", abstract[:4500])
    candidates = [s for s in sentences if 45 <= len(s) <= 360 and re.search(SIGNALS[domain], s, re.I)]
    if candidates:
        best = max(candidates, key=lambda s: (bool(re.search(METHOD, s, re.I)),
                                              len(set(re.findall(SIGNALS[domain], s.lower())))))
        return best, "Нативная аннотация на стр. 1, фрагмент для проверки"
    if pointers:
        return " | ".join(f"стр. {p['page']}: {p['excerpt']}" for p in pointers[:2]), "Указатели P0 №21"
    return "Начать с аннотации и методики; механизм требует визуальной сверки.", "Очередь P0 №21"


def cell(text):
    # Keep imported scientific text inert when a spreadsheet opens the CSV.
    if isinstance(text, str):
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "�", text)
        if text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
    return text


def build_scope_row(row, overrides, root):
    doc_id = row["document_id"]
    override = overrides.get(doc_id, {})
    with pymupdf.open(root / row["pdf_path"]) as pdf:
        title, abstract = front_matter(pdf)
    title = override.get("title", title)
    if not title:
        raise ValueError(f"{doc_id}: title requires a visual check")
    pointers = json.loads(row["native_text_pointers"])
    if row["agent_proposal"] or row["evidence_origin"].startswith("Визуальный"):
        summary, summary_source = row["reason"], row["evidence_origin"]
    else:
        summary, summary_source = abstract_excerpt(row["domain"], abstract, pointers)
    summary = override.get("summary", summary)
    if "summary" in override:
        summary_source = "Краткое описание по PDF и указателям P0 №21"
    pages = sorted(set([1] + json.loads(row["evidence_pages"]) + json.loads(row["review_start_pages"])
                       + override.get("pages", [])))
    output = {name: row[name] for name in (
        "domain", "publication_id", "document_id", "pdf_sha256", "pdf_path",
        "page_count", "preliminary_status",
    )}
    basis = row["reason"]
    if pointers:
        basis = "Предложение не сформулировано. " + " | ".join(
            f"стр. {p['page']}: {p['excerpt']}" for p in pointers)
    # Keep the basis and mechanism distinct even when they share the P0 #21 source.
    if row["agent_proposal"]:
        summary = override.get("summary", re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", summary)[0])
    output.update(title=title, title_source="Первая страница PDF",
                  mechanism_summary=summary, summary_source=summary_source,
                  agent_proposal=row["agent_proposal"] or "no_proposal",
                  scope_basis=basis, review_pages=json.dumps(pages),
                  priority="disputed_rag" if doc_id in DISPUTED else "standard")
    sanitized = {name: cell(value) for name, value in output.items()}
    sanitized.update(dict.fromkeys(INPUTS, ""))
    return sanitized


def build_scope_sheet(output_path, *, queue_path, metadata_path=None, root=Path.cwd(), append=False):
    output_path = Path(output_path)
    queue_path = Path(queue_path)
    root = Path(root)
    if output_path.is_symlink() or output_path.resolve() in {
        path.resolve() for path in (queue_path, Path(metadata_path) if metadata_path else queue_path)
    }:
        raise ValueError("The approval sheet must be separate from its input files")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock = output_path.with_name(f".{output_path.name}.lock")
    with lock.open("x"):
        pass
    try:
        original = output_path.read_bytes() if output_path.exists() else None
        if original is not None and not append:
            raise FileExistsError(f"{output_path}: use --append to add rows to an existing working sheet")
        existing = (read_scope_rows(original, output_path, FIELDS, exact=True)
                    if original is not None else {})
        queue = read_queue(queue_path)
        if not queue:
            raise ValueError(f"{queue_path}: review queue is empty")
        rows = sorted(queue.values(), key=lambda row: (row["document_id"] not in DISPUTED,
                                                        row["domain"], row["publication_id"]))
        updates = []
        for row in rows:
            key = row["document_id"]
            if key in existing and any(existing[key][field] != row[field] for field in IDENTITY):
                raise ValueError(f"{key}: publication metadata or PDF SHA-256 changed since the saved review")
            if pdf_sha256(root / row["pdf_path"]) != row["pdf_sha256"]:
                raise ValueError(f"{key}: actual PDF SHA-256 differs from the review queue")
            if key not in existing:
                updates.append(row)
        if original is not None and not updates:
            return 0, None
        overrides = json.loads(Path(metadata_path).read_text(encoding="utf-8")) if metadata_path else {}
        new_rows = [build_scope_row(row, overrides, root) for row in updates]
        backup = _write_sheet(output_path, [*existing.values(), *new_rows], original)
        return len(new_rows), backup
    finally:
        lock.unlink()


def _write_sheet(output_path, rows, original):
    tmp_path = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8-sig",
            dir=output_path.parent,
            prefix=f".{output_path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            writer = csv.DictWriter(tmp_file, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)

        if original is None:
            os.link(tmp_path, output_path)
            return None
        with NamedTemporaryFile(dir=output_path.parent, prefix=f"{output_path.stem}.backup-",
                                suffix=".csv", delete=False) as backup:
            backup.write(original)
        if output_path.read_bytes() != original:
            raise ValueError(f"{output_path}: review sheet was edited during the build")
        os.replace(tmp_path, output_path)
        return Path(backup.name)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="New CSV or existing working copy")
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--root", default=Path.cwd(), type=Path)
    parser.add_argument("--append", action="store_true", help="Add new rows and back up the working copy")
    args = parser.parse_args(argv)
    try:
        count, backup_path = build_scope_sheet(args.csv, queue_path=args.queue, metadata_path=args.metadata,
                                               root=args.root, append=args.append)
        if backup_path is not None:
            print(f"Created backup: {backup_path}")
        print(f"{args.csv}: added {count} rows")
    except (OSError, ValueError, csv.Error, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
