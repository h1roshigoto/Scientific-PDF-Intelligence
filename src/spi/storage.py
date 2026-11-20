"""Local JSON persistence; technical metadata and scientific records stay separate."""
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import PublicationScope, ResultRecord, ValidationIssue


class Store:
    def __init__(self, root="state"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def write(self, name, data):
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as temp:
            json.dump(data, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            target = temp.name
        os.replace(target, self.root / name)

    @contextmanager
    def _write_lock(self):
        # The SQLite write lock serializes JSON read-modify-write across processes.
        with closing(sqlite3.connect(self.root / "write-lock.sqlite3", timeout=15)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            yield

    def _upsert(self, name, item, key):
        data = [row for row in self.read(name) if row[key] != item[key]]
        data.append(item)
        self.write(name, data)

    def upsert(self, name, item, key):
        with self._write_lock():
            self._upsert(name, item, key)

    def save_document(self, record, pages):
        with self._write_lock():
            self.write(f"{record['domain']}_{record['publication_id']}_pages.json", pages)
            self._upsert("documents.json", record, "document_id")

    def save_extraction(self, result: ResultRecord) -> bool:
        metadata = result.extraction_metadata
        if metadata is None:
            raise ValueError("Extraction metadata is required")
        with self._write_lock():
            if not any(d["document_id"] == result.document_id and d.get("sha256") == metadata.pdf_sha256
                       for d in self.documents()):
                return False
            self._upsert("results.json", result.model_dump(mode="json"), "result_id")
        return True

    def documents(self):
        return self.read("documents.json")

    def publication_scopes(self) -> dict[tuple[str, str], PublicationScope]:
        return {(row.document_id, row.pdf_sha256): row for row in
                map(PublicationScope.model_validate, self.read("publication_scopes.json"))}

    def save_publication_scopes(self, decisions: list[PublicationScope]) -> None:
        updates = {(row.document_id, row.pdf_sha256): row for row in decisions}
        if not updates or len(updates) != len(decisions):
            raise ValueError("Provide one scope decision per document and PDF SHA-256")
        with self._write_lock():
            documents = {doc["document_id"]: doc for doc in self.documents()}
            for decision in decisions:
                doc = documents.get(decision.document_id)
                if doc is None or doc.get("sha256") != decision.pdf_sha256:
                    raise ValueError(f"{decision.document_id}: scope decision does not match an imported PDF SHA-256")
                if any(page > doc["page_count"] for page in decision.source_pages):
                    raise ValueError(f"{decision.document_id}: scope evidence page is outside the PDF")
            scopes = self.publication_scopes()
            scopes.update(updates)
            self.write("publication_scopes.json", [row.model_dump(mode="json") for row in scopes.values()])

    def results(self, domain=None):
        hashes = {d["document_id"]: d.get("sha256") for d in self.documents()}
        return [r for r in map(ResultRecord.model_validate, self.read("results.json"))
                if (domain is None or r.domain == domain)
                and (r.extraction_metadata is None
                     or hashes.get(r.document_id) == r.extraction_metadata.pdf_sha256)]

    def issues(self, domain=None):
        return [r for r in map(ValidationIssue.model_validate, self.read("issues.json"))
                if domain is None or r.domain == domain]
