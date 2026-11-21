"""Versioned contracts shared by the private exporter and offline verifier."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .models import Domain, ProcessingStatus, ResultRecord, UploadProcessing, ValidationIssue
from .review_models import Digest, OpaqueId, ReviewedResult, ReviewEvent, ReviewStatus

PROJECT_FORMAT = "spi-private-project-v1"
CSV_FORMAT = "spi-private-reviewed-results-v1"
REVIEWED_FORMAT = "spi-private-reviewed-results-json-v1"
ISSUES_FORMAT = "spi-private-issues-json-v1"
HISTORY_FORMAT = "spi-private-review-history-json-v1"
ROOT = "spi-project/"
MANIFEST = "manifest.json"
MEMBERS = (
    "README.md", "reviewed_results.csv", "reviewed_results.json", "issues.json",
    "review_history.json", "scientific-passport.pdf",
)
DEFAULT_EXPORT_BYTES = 32 * 1024 * 1024


class ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class PackageMetadata(ExportModel):
    format: Literal["spi-private-project-v1"] = PROJECT_FORMAT
    upload_id: OpaqueId
    domain: Domain
    pdf_sha256: Digest
    original_filename: str = Field(min_length=1, max_length=255)
    processing_job_id: OpaqueId
    processing_status: ProcessingStatus
    exported_at: AwareDatetime
    application_version: str | None
    snapshot_sha256: Digest
    official_submission: Literal[False] = False

    @model_validator(mode="after")
    def validate_capture(self):
        if self.processing_status not in {"completed", "needs_review", "failed"}:
            raise ValueError("Export requires a finished attempt")
        if self.exported_at.utcoffset().total_seconds() != 0:
            raise ValueError("Export time must be in UTC")
        return self


class ExportWarning(ExportModel):
    code: Literal[
        "zero_approved_results", "scientific_completeness_unverified", "incomplete_ocr",
        "local_only", "partial_analysis", "unresolved_issues", "private_not_official_submission",
    ]
    message: str


class ReviewCounts(ExportModel):
    automatic: int = Field(ge=0)
    approved: int = Field(ge=0)
    approved_corrected: int = Field(ge=0)
    pending: int = Field(ge=0)
    correction_proposed: int = Field(ge=0)
    rejected: int = Field(ge=0)
    stale: int = Field(ge=0)
    unconfirmed: int = Field(ge=0)


class IssueCounts(ExportModel):
    total: int = Field(ge=0)
    info: int = Field(ge=0)
    warning: int = Field(ge=0)
    error: int = Field(ge=0)


class ValidationSummary(ExportModel):
    review_counts: ReviewCounts
    issue_counts: IssueCounts
    ocr_pending_pages: tuple[int, ...]
    processing_mode: Literal["local", "gigachat"] | None
    extraction_coverage: str | None
    scientific_completeness: Literal["unverified"] = "unverified"
    user_review_status: Literal["audit_only", "partially_approved", "all_current_results_approved"]
    warnings: tuple[ExportWarning, ...]


class ManifestPreview(ExportModel):
    metadata: PackageMetadata
    validation: ValidationSummary
    available_downloads: tuple[str, ...] = (
        "reviewed-results.csv", "reviewed-results.json", "scientific-passport.pdf", "project.zip",
    )


class FileChecksum(ExportModel):
    name: str
    size_bytes: int = Field(gt=0)
    sha256: Digest


class ExportManifest(ExportModel):
    metadata: PackageMetadata
    validation: ValidationSummary
    csv_format: Literal["spi-private-reviewed-results-v1"] = CSV_FORMAT
    reviewed_results_format: Literal["spi-private-reviewed-results-json-v1"] = REVIEWED_FORMAT
    issues_format: Literal["spi-private-issues-json-v1"] = ISSUES_FORMAT
    review_history_format: Literal["spi-private-review-history-json-v1"] = HISTORY_FORMAT
    checksum_algorithm: Literal["SHA-256"] = "SHA-256"
    manifest_self_excluded: Literal[True] = True
    files: tuple[FileChecksum, ...]

    @model_validator(mode="after")
    def validate_members(self):
        if tuple(entry.name for entry in self.files) != MEMBERS:
            raise ValueError("Unexpected manifest file list")
        return self


class CurrentReview(ExportModel):
    result_id: str
    status: ReviewStatus
    revision: int = Field(ge=0)
    review_id: OpaqueId | None


class AuditEvent(ExportModel):
    applicability: Literal["current", "superseded", "stale"]
    event: ReviewEvent


class ReviewedResultsDocument(ExportModel):
    format: Literal["spi-private-reviewed-results-json-v1"] = REVIEWED_FORMAT
    snapshot_sha256: Digest
    results: tuple[ReviewedResult, ...]


class IssuesDocument(ExportModel):
    format: Literal["spi-private-issues-json-v1"] = ISSUES_FORMAT
    snapshot_sha256: Digest
    review_approval_resolves_issues: Literal[False] = False
    issues: tuple[ValidationIssue, ...]
    processing: UploadProcessing


class ReviewHistoryDocument(ExportModel):
    format: Literal["spi-private-review-history-json-v1"] = HISTORY_FORMAT
    snapshot_sha256: Digest
    automatic_results: tuple[ResultRecord, ...]
    current_reviews: tuple[CurrentReview, ...]
    events: tuple[AuditEvent, ...]


class SnapshotData(ExportModel):
    processing: UploadProcessing
    reviewed_results: tuple[ReviewedResult, ...]
    automatic_results: tuple[ResultRecord, ...]
    issues: tuple[ValidationIssue, ...]
    current_reviews: tuple[CurrentReview, ...]
    events: tuple[AuditEvent, ...]


class FrozenSnapshot(ExportModel):
    preview: ManifestPreview
    # The nested scientific models contain mutable JSON containers. Store the
    # captured payload as bytes so subsequent reads cannot mutate the snapshot.
    payload: bytes


class ExportArtifact(ExportModel):
    filename: str
    media_type: str
    content: bytes
