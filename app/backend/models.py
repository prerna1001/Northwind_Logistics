from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import get_settings
from .database import Base


settings = get_settings()


class Employee(Base):
    __tablename__ = "employees"

    employee_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    grade: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(255))
    manager_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    home_base: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trip_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    trip_dates: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    submissions: Mapped[list["Submission"]] = relationship(back_populates="employee")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.employee_id"))
    source: Mapped[str] = mapped_column(String(32), default="manual")
    sample_case_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employee_info_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trip_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    trip_dates: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="draft")
    last_analysis_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    employee: Mapped[Employee] = relationship(back_populates="submissions")
    receipts: Mapped[list["Receipt"]] = relationship(back_populates="submission", cascade="all, delete-orphan")


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    original_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(32))
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    storage_backend: Mapped[str] = mapped_column(String(64), default="local_s3_mirror")
    s3_key: Mapped[str] = mapped_column(String(500))
    storage_uri: Mapped[str] = mapped_column(String(1000))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    submission: Mapped[Submission] = relationship(back_populates="receipts")
    extraction: Mapped["ReceiptExtraction | None"] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        uselist=False,
    )
    findings: Mapped[list["DeterministicFinding"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
    )
    verdict: Mapped["Verdict | None"] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ReceiptExtraction(Base):
    __tablename__ = "receipt_extractions"
    __table_args__ = (UniqueConstraint("receipt_id", name="uq_receipt_extraction_receipt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"))
    parser_version: Mapped[str] = mapped_column(String(64))
    extraction_status: Mapped[str] = mapped_column(String(64), default="pending")
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_data: Mapped[dict] = mapped_column(JSON, default=dict)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    routing_data: Mapped[dict] = mapped_column(JSON, default=dict)
    needs_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    receipt: Mapped[Receipt] = relationship(back_populates="extraction")


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_name: Mapped[str] = mapped_column(String(255))
    document_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    policy_family: Mapped[str] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_date: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_source_key: Mapped[str] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chunks: Mapped[list["PolicyChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    rules: Mapped[list["PolicyRule"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("policy_documents.id"))
    section_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    part_index: Mapped[int] = mapped_column(Integer, default=1)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    topic_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    chunk_text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dimensions), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped[PolicyDocument] = relationship(back_populates="chunks")


class PolicyRule(Base):
    __tablename__ = "policy_rules"
    __table_args__ = (UniqueConstraint("rule_id", name="uq_policy_rule_rule_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[int] = mapped_column(ForeignKey("policy_documents.id"))
    section_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_type: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    severity_hint: Mapped[str] = mapped_column(String(32))
    quoted_source_text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped[PolicyDocument] = relationship(back_populates="rules")


class DeterministicFinding(Base):
    __tablename__ = "deterministic_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"))
    rule_id: Mapped[str] = mapped_column(String(128))
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    severity_hint: Mapped[str] = mapped_column(String(32))
    matched_facts: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text)
    policy_reference: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    receipt: Mapped[Receipt] = relationship(back_populates="findings")


class Verdict(Base):
    __tablename__ = "verdicts"
    __table_args__ = (UniqueConstraint("receipt_id", name="uq_verdict_receipt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"))
    verdict: Mapped[str] = mapped_column(String(32))
    reasoning_summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_findings: Mapped[list[dict]] = mapped_column(JSON, default=list)
    recommended_action: Mapped[str] = mapped_column(Text)
    human_review_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    model_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    receipt: Mapped[Receipt] = relationship(back_populates="verdict")
    overrides: Mapped[list["ReviewOverride"]] = relationship(back_populates="verdict_record", cascade="all, delete-orphan")


class ReviewOverride(Base):
    __tablename__ = "review_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    verdict_id: Mapped[int] = mapped_column(ForeignKey("verdicts.id"))
    reviewer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_verdict: Mapped[str] = mapped_column(String(32))
    override_verdict: Mapped[str] = mapped_column(String(32))
    reviewer_comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    verdict_record: Mapped[Verdict] = relationship(back_populates="overrides")
