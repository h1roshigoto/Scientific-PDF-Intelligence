"""Internal API contract. Official submission CSV column names come from the supplied templates."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Domain = Literal["solar", "rag"]
ExtractionStatus = Literal["extracted", "needs_review", "verified"]
ScopeStatus = Literal["in_scope", "out_of_scope", "needs_review"]


class PublicationScope(BaseModel):
    """A scope review for one version of an imported PDF."""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    document_id: str = Field(pattern=r"^(solar|rag):[A-Za-z0-9][A-Za-z0-9_-]*$")
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ScopeStatus
    reason: str | None = Field(default=None, min_length=1)
    source_pages: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_review_evidence(self):
        if self.status != "needs_review" and (not self.reason or not self.source_pages):
            raise ValueError("A confirmed scope decision requires a reason and PDF source pages")
        if any(page < 1 for page in self.source_pages):
            raise ValueError("PDF source pages are 1-based")
        return self


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | float | None = None
    unit: str | None = None


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    k: int | None = Field(default=None, ge=0)
    variant: str | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1)
    fragment: str | None = None
    section: str | None = None
    table: str | None = None
    figure: str | None = None
    row: str | None = None
    column: str | None = None
    origin: Literal["primary", "cited"] | None = None
    cited_publication_id: str | None = None

    @model_validator(mode="after")
    def require_location(self):
        if not any([self.fragment, self.table, self.figure, self.section]):
            raise ValueError("evidence needs a fragment, table, figure, or section")
        return self


class ExtractionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str
    method: Literal["pymupdf_gigachat_json_schema"]
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_version: str
    profile: str
    source_block_id: str | None = None


class ResultRecord(BaseModel):
    """A value is never promoted to verified merely because it was parsed."""
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    domain: Domain
    metric: Metric
    raw: Value
    normalized: Value
    source: Evidence
    context: dict[str, Any] = Field(default_factory=dict)
    extraction_status: ExtractionStatus = "needs_review"
    note: str | None = None
    extraction_metadata: ExtractionMetadata | None = None

    @model_validator(mode="after")
    def require_raw(self):
        if self.raw.value is None:
            raise ValueError("raw.value is required; unknown values must not become results")
        if not self.document_id.startswith(self.domain + ":"):
            raise ValueError("document_id must have the correct domain prefix")
        return self


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(min_length=1)
    domain: Domain
    rule_id: str = Field(min_length=1)
    severity: Literal["info", "warning", "error"] = "warning"
    result_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    observed: str | None = None
    expected: str | None = None
