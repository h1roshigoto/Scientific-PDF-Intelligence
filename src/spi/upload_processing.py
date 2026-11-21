"""Access the existing scientific Store inside one private upload namespace."""

from pathlib import Path

from .models import StoredUpload, ValidationIssue
from .storage import Store
from .upload_store import private_path, private_sqlite_path


class UploadProcessingStore(Store):
    def __init__(self, root: Path, upload: StoredUpload, *, create: bool = False):
        self.upload = upload
        self.document_id = f"{upload.domain}:{upload.upload_id}"
        self.root = private_path(root, Path(upload.upload_id), directory=True)
        if create:
            super().__init__(self.root)

    def _path(self, name):
        name = Path(name)
        if name.suffix == ".sqlite3":
            return private_sqlite_path(self.root, name)
        return private_path(self.root, name)

    def save_document(self, record, pages):
        if not self._matches(record):
            raise ValueError("Processing store document identity mismatch")
        super().save_document({**record, "original_filename": self.upload.original_filename}, pages)

    def _matches(self, record):
        return (record.get("document_id"), record.get("domain"), record.get("sha256")) == (
            self.document_id, self.upload.domain, self.upload.sha256,
        )

    def batch_documents(self, domain):
        if domain != self.upload.domain:
            return []
        record = self.read(Path("batch") / domain / f"{self.upload.upload_id}.json")
        return [record] if record else []

    def checkpoint(self):
        records = self.batch_documents(self.upload.domain)
        return records[0] if records and self._matches(records[0]) else None

    def upload_results(self):
        # The batch verifies source alignment. Human verification of a private
        # record is a separate decision that this worker never makes.
        return [
            row.model_copy(update={"extraction_status": "extracted"})
            if row.extraction_status == "verified" else row
            for row in self.results(self.upload.domain)
            if row.document_id == self.document_id and row.extraction_metadata is not None
            and row.extraction_metadata.pdf_sha256 == self.upload.sha256
            and row.source.page <= self.upload.page_count
        ]

    def upload_issues(self, record):
        domain = self.upload.domain
        issues = [issue for owner, issue in self.issue_entries(domain) if owner == self.document_id]
        unresolved = 0

        def add(kind, key, reason, page, *, result_ids=(), **evidence):
            issues.append(ValidationIssue(
                issue_id=f"{self.document_id}:{kind}:{key}", domain=domain, rule_id=kind,
                reason=reason, result_ids=list(result_ids),
                evidence_refs=[{"document_id": self.document_id, "page": page, **evidence}],
            ))

        for index, problem in enumerate(record.get("layout_issues", ())):
            add("table_layout_unresolved", index, problem["reason"], problem["page"],
                table=problem.get("location"), fragment=problem.get("quote"),
                source_block_id=problem.get("source_block_id"))
            unresolved += 1
        for key, fragment in record.get("fragments", {}).items():
            if fragment["status"] == "review":
                ids = [row["result_id"] for row in fragment.get("results", ())]
                add("fragment_needs_review", key, " | ".join(fragment["issues"]), fragment["page"],
                    result_ids=ids, table=fragment.get("location"), details=fragment.get("review_details", []))
                unresolved += 1
        for page in record.get("pages", ()):
            if page.get("error") or page.get("analysis_error"):
                add("page_analysis_failed", page["page"], "page_analysis_failed", page["page"])
                unresolved += 1
        return issues, unresolved
