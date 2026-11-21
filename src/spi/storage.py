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

    def _path(self, name):
        return self.root / name

    def read(self, name):
        path = self._path(name)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def write(self, name, data):
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temp:
                temporary = Path(temp.name)
                json.dump(data, temp, ensure_ascii=False, indent=2)
                temp.write("\n")
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def _write_lock(self):
        # The SQLite write lock serializes JSON read-modify-write across processes.
        with closing(sqlite3.connect(self._path("write-lock.sqlite3"), timeout=15)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            yield

    @contextmanager
    def batch_lock(self):
        # Held across parsing and model calls. A terminated process releases the lock.
        with closing(sqlite3.connect(self._path("batch-lock.sqlite3"), timeout=0)) as lock, lock:
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

    def batch_documents(self, domain):
        return [self.read(path.relative_to(self.root))
                for path in sorted((self.root / "batch" / domain).glob("*.json"))]

    def save_batch_document(self, record):
        domain, publication = record["document_id"].split(":", 1)
        self.write(Path("batch") / domain / f"{publication}.json", record)

    def publication_scopes(self) -> dict[tuple[str, str], PublicationScope]:
        rows = self.read("publication_scopes.json")
        scopes = {(row.document_id, row.pdf_sha256): row for row in
                  map(PublicationScope.model_validate, rows)}
        if len(rows) != len(scopes):
            raise ValueError("Store contains duplicate scope decisions for the same PDF")
        return scopes

    def _excluded_documents(self, hashes):
        return {document_id for (document_id, digest), scope in self.publication_scopes().items()
                if scope.status == "out_of_scope" and hashes.get(document_id) == digest}

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

    def results(self, domain=None, *, include_excluded=True):
        hashes = {d["document_id"]: d.get("sha256") for d in self.documents()}
        excluded = set() if include_excluded else self._excluded_documents(hashes)
        rows = {r["result_id"]: r for r in self.read("results.json")}
        for track in (domain,) if domain else ("solar", "rag"):
            for doc in self.batch_documents(track):
                if doc["document_id"] in excluded or doc.get("sha256") != hashes.get(doc["document_id"]):
                    continue
                for fragment in doc.get("fragments", {}).values():
                    rows.update((r["result_id"], r) for r in fragment.get("results", []))
        return [r for r in map(ResultRecord.model_validate, rows.values())
                if (domain is None or r.domain == domain)
                and r.document_id not in excluded
                and (r.extraction_metadata is None
                     or hashes.get(r.document_id) == r.extraction_metadata.pdf_sha256)]

    def issues(self, domain=None, *, include_excluded=True):
        return [issue for _, issue in self.issue_entries(domain, include_excluded=include_excluded)]

    def issue_entries(self, domain=None, *, include_excluded=True):
        """Yield issues with their checkpoint owner, when stored in a document checkpoint."""
        hashes = {d["document_id"]: d.get("sha256") for d in self.documents()}
        excluded = set() if include_excluded else self._excluded_documents(hashes)
        for issue in self.read("issues.json"):
            owners = {ref["document_id"] for ref in issue.get("evidence_refs", ()) if ref.get("document_id")}
            if not owners or not owners <= excluded:
                row = ValidationIssue.model_validate(issue)
                if domain is None or row.domain == domain:
                    yield None, row
        for track in (domain,) if domain else ("solar", "rag"):
            for doc in self.batch_documents(track):
                if doc["document_id"] not in excluded and doc.get("sha256") == hashes.get(doc["document_id"]):
                    for issue in doc.get("science_issues", ()):
                        row = ValidationIssue.model_validate(issue)
                        if domain is None or row.domain == domain:
                            yield doc["document_id"], row
