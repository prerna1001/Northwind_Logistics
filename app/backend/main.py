from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .analysis import analyze_submission, answer_chat_question
from .config import get_settings
from .database import SessionLocal, get_db
from .extraction import detect_file_type, paddleocr_available, process_uploaded_receipt
from .models import (
    DeterministicFinding,
    Employee,
    PolicyChunk,
    PolicyDocument,
    Receipt,
    ReceiptExtraction,
    ReviewOverride,
    Submission,
    Verdict,
)
from .schemas import BootstrapStatus, ChatRequest, OverrideCreate, SubmissionCreate
from .seed import bootstrap_data


settings = get_settings()


def serialize_receipt(receipt: Receipt) -> dict:
    extraction = receipt.extraction
    verdict = receipt.verdict
    latest_override = verdict.overrides[-1] if verdict and verdict.overrides else None
    effective_verdict = latest_override.override_verdict if latest_override else (verdict.verdict if verdict else None)
    return {
        "id": receipt.id,
        "original_filename": receipt.original_filename,
        "file_type": receipt.file_type,
        "source": receipt.source,
        "storage_backend": receipt.storage_backend,
        "storage_uri": receipt.storage_uri,
        "category": extraction.category if extraction else None,
        "extraction_status": extraction.extraction_status if extraction else None,
        "extraction_confidence": extraction.extraction_confidence if extraction else None,
        "normalized_data": extraction.normalized_data if extraction else {},
        "routing_data": extraction.routing_data if extraction else {},
        "deterministic_findings": [
            {
                "rule_id": finding.rule_id,
                "triggered": finding.triggered,
                "severity_hint": finding.severity_hint,
                "summary": finding.summary,
                "matched_facts": finding.matched_facts,
                "policy_reference": finding.policy_reference,
            }
            for finding in receipt.findings
        ],
        "system_verdict": None
        if verdict is None
        else {
            "verdict": verdict.verdict,
            "reasoning_summary": verdict.reasoning_summary,
            "confidence": verdict.confidence,
            "policy_findings": verdict.policy_findings,
            "recommended_action": verdict.recommended_action,
            "human_review_needed": verdict.human_review_needed,
            "model_name": verdict.model_name,
            "created_at": verdict.created_at.isoformat(),
        },
        "latest_override": None
        if latest_override is None
        else {
            "override_verdict": latest_override.override_verdict,
            "reviewer_comment": latest_override.reviewer_comment,
            "reviewer_name": latest_override.reviewer_name,
            "created_at": latest_override.created_at.isoformat(),
        },
        "effective_verdict": effective_verdict,
        "uploaded_at": receipt.uploaded_at.isoformat(),
    }


def serialize_submission(submission: Submission) -> dict:
    return {
        "id": submission.id,
        "employee_id": submission.employee_id,
        "source": submission.source,
        "sample_case_id": submission.sample_case_id,
        "trip_purpose": submission.trip_purpose,
        "trip_dates": submission.trip_dates,
        "status": submission.status,
        "deleted_at": submission.deleted_at.isoformat() if submission.deleted_at else None,
        "last_analysis_at": submission.last_analysis_at.isoformat() if submission.last_analysis_at else None,
        "created_at": submission.created_at.isoformat(),
        "employee": {
            "employee_id": submission.employee.employee_id,
            "name": submission.employee.name,
            "grade": submission.employee.grade,
            "title": submission.employee.title,
            "department": submission.employee.department,
            "manager_id": submission.employee.manager_id,
            "home_base": submission.employee.home_base,
            "trip_purpose": submission.employee.trip_purpose,
            "trip_dates": submission.employee.trip_dates,
            "source": submission.employee.source,
        },
        "receipts": [serialize_receipt(receipt) for receipt in sorted(submission.receipts, key=lambda item: item.id)],
    }


def get_submission_or_404(submission_id: int, db: Session) -> Submission:
    stmt = (
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.employee),
            selectinload(Submission.receipts).selectinload(Receipt.extraction),
            selectinload(Submission.receipts).selectinload(Receipt.findings),
            selectinload(Submission.receipts).selectinload(Receipt.verdict).selectinload(Verdict.overrides),
        )
    )
    submission = db.scalars(stmt).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


@asynccontextmanager
async def lifespan(_: FastAPI):
    with SessionLocal() as db:
        bootstrap_data(db)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "database": "postgresql", "vector_store": "pgvector"}


@app.get("/api/bootstrap-status", response_model=BootstrapStatus)
def get_bootstrap_status(db: Session = Depends(get_db)):
    return BootstrapStatus(
        employees_seeded=db.scalar(select(func.count()).select_from(Employee).where(Employee.source == "sample")) or 0,
        policy_documents=db.scalar(select(func.count()).select_from(PolicyDocument)) or 0,
        policy_chunks=db.scalar(select(func.count()).select_from(PolicyChunk)) or 0,
        sample_submissions=db.scalar(select(func.count()).select_from(Submission).where(Submission.source == "sample")) or 0,
        storage_backend=settings.storage_backend_label,
        paddleocr_available=paddleocr_available(),
        llama_configured=bool(settings.resolved_llama_api_url and settings.llama_api_token),
    )


@app.get("/api/employees")
def list_employees(db: Session = Depends(get_db)):
    employees = db.scalars(select(Employee).order_by(Employee.name.asc())).all()
    return [
        {
            "employee_id": employee.employee_id,
            "name": employee.name,
            "grade": employee.grade,
            "title": employee.title,
            "department": employee.department,
            "manager_id": employee.manager_id,
            "home_base": employee.home_base,
            "trip_purpose": employee.trip_purpose,
            "trip_dates": employee.trip_dates,
            "source": employee.source,
        }
        for employee in employees
    ]


@app.post("/api/employees")
def create_employee(payload: dict, db: Session = Depends(get_db)):
    if db.get(Employee, payload["employee_id"]):
        raise HTTPException(status_code=409, detail="Employee already exists")
    employee = Employee(**payload, source="manual")
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return {"employee_id": employee.employee_id}


@app.post("/api/submissions")
def create_submission(payload: SubmissionCreate, db: Session = Depends(get_db)):
    employee = db.get(Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    submission = Submission(
        employee_id=payload.employee_id,
        source="manual",
        trip_purpose=payload.trip_purpose or employee.trip_purpose,
        trip_dates=payload.trip_dates or employee.trip_dates,
        status="draft",
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return serialize_submission(get_submission_or_404(submission.id, db))


@app.get("/api/submissions")
def list_submissions(
    source: str | None = Query(default=None),
    employee: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Submission)
        .options(
            selectinload(Submission.employee),
            selectinload(Submission.receipts).selectinload(Receipt.extraction),
            selectinload(Submission.receipts).selectinload(Receipt.findings),
            selectinload(Submission.receipts).selectinload(Receipt.verdict).selectinload(Verdict.overrides),
        )
        .order_by(Submission.created_at.desc())
    )
    if include_deleted:
        stmt = stmt.where(Submission.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Submission.deleted_at.is_(None))
    if source:
        stmt = stmt.where(Submission.source == source)
    if employee:
        stmt = stmt.join(Submission.employee).where(Employee.name.ilike(f"%{employee}%"))
    if status:
        stmt = stmt.where(Submission.status == status)
    if date_from:
        stmt = stmt.where(func.date(Submission.created_at) >= date_from)
    if date_to:
        stmt = stmt.where(func.date(Submission.created_at) <= date_to)
    submissions = db.scalars(stmt).all()
    return [serialize_submission(submission) for submission in submissions]


@app.get("/api/submissions/{submission_id}")
def get_submission(submission_id: int, db: Session = Depends(get_db)):
    return serialize_submission(get_submission_or_404(submission_id, db))


@app.delete("/api/submissions/{submission_id}")
def delete_submission(submission_id: int, db: Session = Depends(get_db)):
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if submission.source != "manual":
        raise HTTPException(status_code=400, detail="Only manually created submissions can be deleted.")
    if submission.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Submission is already in trash.")
    employee_id = submission.employee_id
    submission.deleted_at = datetime.utcnow()
    submission.status = "trashed"
    db.commit()

    remaining_manual_submissions = db.scalar(
        select(func.count()).select_from(Submission).where(
            Submission.employee_id == employee_id,
            Submission.source == "manual",
            Submission.deleted_at.is_(None),
        )
    ) or 0
    return {
        "deleted": True,
        "submission_id": submission_id,
        "employee_id": employee_id,
        "remaining_manual_submissions": remaining_manual_submissions,
    }


@app.post("/api/submissions/{submission_id}/restore")
def restore_submission(submission_id: int, db: Session = Depends(get_db)):
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if submission.source != "manual":
        raise HTTPException(status_code=400, detail="Only manually created submissions can be restored.")
    if submission.deleted_at is None:
        raise HTTPException(status_code=400, detail="Submission is not in trash.")

    submission.deleted_at = None
    submission.status = "draft" if not submission.receipts else "ready_for_analysis"
    db.commit()
    return serialize_submission(get_submission_or_404(submission_id, db))


@app.post("/api/submissions/{submission_id}/receipts")
def upload_receipts(
    submission_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    submission = get_submission_or_404(submission_id, db)
    if submission.source == "sample":
        raise HTTPException(status_code=400, detail="Sample cases already include their receipts.")
    for upload in files:
        stored, file_type = process_uploaded_receipt(upload, submission.employee_id, submission.id)
        db.add(
            Receipt(
                submission_id=submission.id,
                original_filename=upload.filename or "receipt",
                file_type=file_type,
                mime_type=upload.content_type,
                source="manual",
                storage_backend=stored.backend,
                s3_key=stored.s3_key,
                storage_uri=stored.uri,
            )
        )
    submission.status = "ready_for_analysis"
    db.commit()
    return serialize_submission(get_submission_or_404(submission_id, db))


@app.post("/api/submissions/{submission_id}/analyze")
def do_analysis(submission_id: int, db: Session = Depends(get_db)):
    try:
        analyze_submission(db, submission_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc
    return serialize_submission(get_submission_or_404(submission_id, db))


@app.post("/api/receipts/{receipt_id}/override")
def override_receipt(receipt_id: int, payload: OverrideCreate, db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if receipt is None or receipt.verdict is None:
        raise HTTPException(status_code=404, detail="Receipt verdict not found")
    db.add(
        ReviewOverride(
            verdict_id=receipt.verdict.id,
            reviewer_name=payload.reviewer_name or settings.local_reviewer_name,
            original_verdict=receipt.verdict.verdict,
            override_verdict=payload.override_verdict,
            reviewer_comment=payload.reviewer_comment,
        )
    )
    if payload.override_verdict == "needs_human_review":
        receipt.submission.status = "needs_human_review"
    db.commit()
    return serialize_submission(get_submission_or_404(receipt.submission_id, db))


@app.post("/api/chat")
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    if payload.scope == "case" and payload.submission_id is None:
        raise HTTPException(status_code=400, detail="submission_id is required for case-grounded chat")
    try:
        return answer_chat_question(db, payload.scope, payload.question, payload.submission_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Chat failed: {exc}") from exc
