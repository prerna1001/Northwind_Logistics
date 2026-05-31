from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    grade: int
    title: str
    department: str
    manager_id: str | None = None
    home_base: str | None = None
    trip_purpose: str | None = None
    trip_dates: str | None = None


class SubmissionCreate(BaseModel):
    employee_id: str
    trip_purpose: str | None = None
    trip_dates: str | None = None


class OverrideCreate(BaseModel):
    override_verdict: Literal["compliant", "flagged", "rejected", "needs_human_review"]
    reviewer_comment: str
    reviewer_name: str | None = None


class ChatRequest(BaseModel):
    scope: Literal["policy", "case"]
    question: str
    submission_id: int | None = None


class HistoryFilters(BaseModel):
    employee: str | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    source: Literal["sample", "manual"] | None = None


class BootstrapStatus(BaseModel):
    employees_seeded: int
    policy_documents: int
    policy_chunks: int
    sample_submissions: int
    storage_backend: str
    paddleocr_available: bool
    llama_configured: bool


class ConfidenceBreakdown(BaseModel):
    extraction: float
    retrieval: float
    decision: float
    overall: float
    band: Literal["high", "medium", "low"]
    reasons: list[str] = Field(default_factory=list)


class ReceiptView(BaseModel):
    id: int
    original_filename: str
    file_type: str
    source: str
    storage_backend: str
    storage_uri: str
    category: str | None = None
    extraction_status: str | None = None
    extraction_confidence: float | None = None
    normalized_data: dict[str, Any] = Field(default_factory=dict)
    routing_data: dict[str, Any] = Field(default_factory=dict)
    deterministic_findings: list[dict[str, Any]] = Field(default_factory=list)
    system_verdict: dict[str, Any] | None = None
    latest_override: dict[str, Any] | None = None
    effective_verdict: str | None = None
    uploaded_at: datetime


class SubmissionView(BaseModel):
    id: int
    employee_id: str
    source: str
    sample_case_id: str | None = None
    trip_purpose: str | None = None
    trip_dates: str | None = None
    status: str
    last_analysis_at: datetime | None = None
    created_at: datetime
    employee: dict[str, Any]
    receipts: list[ReceiptView] = Field(default_factory=list)


class ChatResponse(BaseModel):
    scope: Literal["policy", "case"]
    grounded: bool
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: dict[str, Any] = Field(default_factory=dict)
