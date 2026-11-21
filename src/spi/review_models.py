"""Contracts for private review decisions, kept separate from extraction status."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .models import UPLOAD_ID_PATTERN, Domain, Evidence, ProcessingStatus, ResultRecord, UploadProcessing, ValidationIssue

ReviewAction = Literal["approve", "propose_correction", "approve_correction", "reject"]
DecisionStatus = Literal["correction_proposed", "approved", "approved_corrected", "rejected"]
ReviewStatus = Literal["pending", "stale", "correction_proposed", "approved", "approved_corrected", "rejected"]
EvidenceMode = Literal["text", "visual"]
PdfStatus = Literal["valid", "missing", "mismatch"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
OpaqueId = Annotated[str, Field(pattern=UPLOAD_ID_PATTERN)]
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ReviewBasis(ReviewModel):
    upload_id: OpaqueId
    domain: Domain
    result_id: str = Field(min_length=1)
    source_job_id: OpaqueId
    pdf_sha256: Digest
    source_result_hash: Digest


class ReviewValue(ReviewModel):
    value: Text | float | None
    unit: Text | None


class ReviewMetric(ReviewModel):
    name: Text
    k: int | None = Field(ge=0)


class ReviewEvidence(Evidence, ReviewModel):
    @model_validator(mode="after")
    def validate_location_and_origin(self):
        if not any(value and value.strip() for value in (self.fragment, self.table, self.figure, self.section)):
            raise ValueError("Evidence requires a nonblank location")
        if self.origin == "cited":
            if not self.cited_publication_id or not self.cited_publication_id.strip():
                raise ValueError("Cited evidence requires a cited publication")
        elif self.cited_publication_id is not None:
            raise ValueError("A cited publication requires cited origin")
        return self


class ScientificCorrection(ReviewModel):
    raw: ReviewValue | None = None
    normalized: ReviewValue | None = None
    metric: ReviewMetric | None = None
    context: dict[str, JsonValue] | None = None
    source: ReviewEvidence | None = None
    normalization_evidence: Text | None = None

    @model_validator(mode="after")
    def require_scientific_fields(self):
        if all(field is None for field in (self.raw, self.normalized, self.metric, self.context, self.source)):
            raise ValueError("A correction needs scientific changes")
        return self


class ReviewRequest(ReviewModel):
    basis: ReviewBasis
    expected_revision: int = Field(ge=0)
    action: ReviewAction
    reviewer_label: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    reason: Text | None = None
    correction: ScientificCorrection | None = None
    evidence_mode: EvidenceMode | None = None
    warnings_acknowledged: bool = False

    @model_validator(mode="after")
    def validate_action(self):
        approval = self.action in {"approve", "approve_correction"}
        if approval != (self.evidence_mode is not None):
            raise ValueError("Approvals require an evidence mode")
        if (self.action == "propose_correction") != (self.correction is not None):
            raise ValueError("Only a proposal can supply scientific changes")
        if self.action in {"propose_correction", "reject"} or self.evidence_mode == "visual":
            if not self.reason or not self.reason.strip():
                raise ValueError("This action requires a reason")
        if self.reviewer_label is not None and not self.reviewer_label.strip():
            raise ValueError("An operator label cannot be blank")
        return self


class ReviewDecision(ReviewBasis):
    review_id: OpaqueId
    revision: int = Field(ge=1)
    status: DecisionStatus
    reviewed_at: AwareDatetime
    reviewer_label: str | None = None
    reason: str | None = None
    evidence_mode: EvidenceMode | None = None
    warnings_acknowledged: bool = False
    normalization_evidence: str | None = None
    reviewed_result: ResultRecord | None = None

    @model_validator(mode="after")
    def validate_decision(self):
        corrected = self.status in {"correction_proposed", "approved_corrected"}
        if corrected != (self.reviewed_result is not None):
            raise ValueError("A corrected decision needs its scientific record")
        if (self.status in {"approved", "approved_corrected"}) != (self.evidence_mode is not None):
            raise ValueError("An approval needs its evidence mode")
        return self


class ReviewEvent(ReviewModel):
    action: ReviewAction
    previous_revision: int = Field(ge=0)
    revision: int = Field(ge=1)
    original_result: ResultRecord
    previous_decision: ReviewDecision | None
    decision: ReviewDecision
    correction: ScientificCorrection | None = None
    issues: list[ValidationIssue]
    ocr_pages: list[int]

    @model_validator(mode="after")
    def validate_revisions(self):
        previous = self.previous_decision.revision if self.previous_decision else 0
        if self.previous_revision != previous or self.revision != previous + 1 or self.decision.revision != self.revision:
            raise ValueError("The event must advance the previous decision by one revision")
        return self


class ReviewState(ReviewModel):
    result_id: str
    original_result: ResultRecord | None
    basis: ReviewBasis | None
    status: ReviewStatus
    revision: int = Field(ge=0)
    applicable: bool
    decision: ReviewDecision | None


class ReviewOverview(ReviewModel):
    processing: UploadProcessing
    pdf_status: PdfStatus
    reviews: list[ReviewState]
    issues: list[ValidationIssue]


class ReviewDetail(ReviewState):
    processing_status: ProcessingStatus
    pdf_status: PdfStatus
    issues: list[ValidationIssue]
    ocr_pages: list[int]


class ReviewedResult(ReviewModel):
    original_result: ResultRecord
    reviewed_result: ResultRecord
    decision: ReviewDecision
    issues: list[ValidationIssue]
    ocr_pages: list[int]
