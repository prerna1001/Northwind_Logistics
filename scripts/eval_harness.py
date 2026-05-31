from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.backend.analysis import analyze_submission, answer_chat_question
from app.backend.database import SessionLocal
from app.backend.models import Employee, Receipt, Submission, Verdict
from app.backend.seed import bootstrap_data
from app.backend.storage import store_file, store_json


@dataclass
class MetricBucket:
    correct: int = 0
    total: int = 0

    def add(self, is_correct: bool | None) -> None:
        if is_correct is None:
            return
        self.total += 1
        if is_correct:
            self.correct += 1

    def as_dict(self) -> dict[str, Any]:
        rate = round(self.correct / self.total, 3) if self.total else None
        return {"correct": self.correct, "total": self.total, "rate": rate}


def _stable_eval_employee_id(source: str) -> str:
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    return f"eval{digest}"[:32]


def _stable_eval_submission_key(source: str) -> str:
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    return f"eval::{digest}"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _serialize_citations(policy_findings: list[dict] | None) -> list[dict[str, Any]]:
    return [
        {
            "policy_title": item.get("policy_title"),
            "document_code": item.get("document_code"),
            "section_key": item.get("section_key"),
            "quote": item.get("quote"),
        }
        for item in (policy_findings or [])
    ]


def _normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _contains_any(text: str | None, expected_terms: list[str]) -> bool:
    haystack = _normalize_text(text)
    return any(term.lower() in haystack for term in expected_terms)


def _citations_match(citations: list[dict[str, Any]], expected: dict[str, Any]) -> bool | None:
    document_codes = expected.get("document_codes_any") or []
    title_terms = expected.get("policy_title_contains_any") or []
    quote_terms = expected.get("quote_terms_any") or []

    if not document_codes and not title_terms and not quote_terms:
        return None

    for citation in citations:
        code_ok = not document_codes or citation.get("document_code") in document_codes
        title_ok = not title_terms or _contains_any(citation.get("policy_title"), title_terms)
        quote_ok = not quote_terms or _contains_any(citation.get("quote"), quote_terms)
        if code_ok and title_ok and quote_ok:
            return True
    return False


def _refusal_match(answer: str, grounded: bool, expected: dict[str, Any]) -> bool | None:
    refusal_expected = expected.get("refusal_expected")
    if refusal_expected is None:
        return None
    refusal_markers = [
        "i can only answer from the policy library",
        "could not find strong enough policy support",
        "decline",
    ]
    did_refuse = (not grounded) and any(marker in _normalize_text(answer) for marker in refusal_markers)
    return did_refuse == bool(refusal_expected)


def _answer_relevance_match(answer: str, expected: dict[str, Any]) -> bool | None:
    expected_terms = expected.get("answer_terms_any") or []
    if not expected_terms:
        return None
    return _contains_any(answer, expected_terms)


def _receipt_query(db, submission_id: int) -> Submission | None:
    return db.scalars(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.employee),
            selectinload(Submission.receipts).selectinload(Receipt.extraction),
            selectinload(Submission.receipts).selectinload(Receipt.findings),
            selectinload(Submission.receipts).selectinload(Receipt.verdict).selectinload(Verdict.overrides),
        )
    ).first()


def _ensure_eval_submission_from_dir(db, submission_dir: Path) -> Submission:
    employee_info_path = submission_dir / "employee_info.json"
    receipts_dir = submission_dir / "receipts"
    if not employee_info_path.exists() or not receipts_dir.exists():
        raise ValueError(f"Submission dir must contain employee_info.json and receipts/: {submission_dir}")

    payload = _load_json(employee_info_path)
    eval_key = _stable_eval_submission_key(str(submission_dir.resolve()))
    employee_id = _stable_eval_employee_id(eval_key)

    existing = db.scalars(
        select(Submission)
        .where(Submission.source == "eval", Submission.sample_case_id == eval_key)
        .options(selectinload(Submission.receipts))
    ).first()
    if existing is not None:
        db.delete(existing)
        db.flush()

    employee = db.get(Employee, employee_id)
    if employee is None:
        employee = Employee(
            employee_id=employee_id,
            name=payload["name"],
            grade=int(payload["grade"]),
            title=payload["title"],
            department=payload["department"],
            manager_id=payload.get("manager_id"),
            home_base=payload.get("home_base"),
            trip_purpose=payload.get("trip_purpose"),
            trip_dates=payload.get("trip_dates"),
            source="eval",
        )
        db.add(employee)
        db.flush()
    else:
        employee.name = payload["name"]
        employee.grade = int(payload["grade"])
        employee.title = payload["title"]
        employee.department = payload["department"]
        employee.manager_id = payload.get("manager_id")
        employee.home_base = payload.get("home_base")
        employee.trip_purpose = payload.get("trip_purpose")
        employee.trip_dates = payload.get("trip_dates")
        employee.source = "eval"

    employee_store = store_json(f"cases/eval/{employee_id}/employee_info.json", payload)
    submission = Submission(
        employee_id=employee.employee_id,
        source="eval",
        sample_case_id=eval_key,
        employee_info_s3_key=employee_store.s3_key,
        trip_purpose=payload.get("trip_purpose"),
        trip_dates=payload.get("trip_dates"),
        status="seeded_eval",
    )
    db.add(submission)
    db.flush()

    for receipt_path in sorted(receipts_dir.glob("*")):
        if not receipt_path.is_file() or receipt_path.name.startswith("."):
            continue
        stored = store_file(f"cases/eval/{employee_id}/receipts/{receipt_path.name}", receipt_path)
        db.add(
            Receipt(
                submission_id=submission.id,
                original_filename=receipt_path.name,
                file_type=receipt_path.suffix.lower().lstrip(".") or "unknown",
                mime_type=None,
                source="eval",
                storage_backend=stored.backend,
                s3_key=stored.s3_key,
                storage_uri=stored.uri,
            )
        )
    db.commit()
    return _receipt_query(db, submission.id)


def _resolve_submission(db, case: dict[str, Any]) -> Submission:
    if case.get("sample_case_id"):
        submission = db.scalars(
            select(Submission)
            .where(Submission.sample_case_id == case["sample_case_id"])
            .options(
                selectinload(Submission.employee),
                selectinload(Submission.receipts).selectinload(Receipt.extraction),
                selectinload(Submission.receipts).selectinload(Receipt.findings),
                selectinload(Submission.receipts).selectinload(Receipt.verdict).selectinload(Verdict.overrides),
            )
        ).first()
        if submission is None:
            raise ValueError(f"Sample case not found: {case['sample_case_id']}")
        return submission
    if case.get("submission_id") is not None:
        submission = _receipt_query(db, int(case["submission_id"]))
        if submission is None:
            raise ValueError(f"Submission not found: {case['submission_id']}")
        return submission
    if case.get("submission_dir"):
        return _ensure_eval_submission_from_dir(db, Path(case["submission_dir"]).expanduser().resolve())
    raise ValueError("Each receipt or case chat case must include sample_case_id, submission_id, or submission_dir")


def _evaluate_receipt_case(db, case: dict[str, Any]) -> dict[str, Any]:
    submission = _resolve_submission(db, case)
    analyze_submission(db, submission.id)
    submission = _receipt_query(db, submission.id)

    receipt_name = case.get("receipt_filename")
    if not receipt_name:
        raise ValueError("receipt_review case requires receipt_filename")
    receipt = next((item for item in submission.receipts if item.original_filename == receipt_name), None)
    if receipt is None:
        raise ValueError(f"Receipt not found in submission: {receipt_name}")
    if receipt.extraction is None or receipt.verdict is None:
        raise ValueError(f"Receipt was not analyzed correctly: {receipt_name}")

    expected = case.get("expected", {})
    citations = _serialize_citations(receipt.verdict.policy_findings)
    actual = {
        "submission_id": submission.id,
        "employee_name": submission.employee.name,
        "receipt_filename": receipt.original_filename,
        "category": receipt.extraction.category,
        "verdict": receipt.verdict.verdict,
        "human_review_needed": receipt.verdict.human_review_needed,
        "extraction_confidence": receipt.extraction.extraction_confidence,
        "confidence_band": (receipt.verdict.confidence or {}).get("band"),
        "citations": citations,
    }
    checks = {
        "verdict": None if "verdict" not in expected else (actual["verdict"] == expected["verdict"]),
        "category": None if "category" not in expected else (actual["category"] == expected["category"]),
        "human_review_needed": None
        if "human_review_needed" not in expected
        else (actual["human_review_needed"] == expected["human_review_needed"]),
        "retrieval_quality": _citations_match(citations, expected),
        "citation_correctness": _citations_match(citations, expected),
    }
    specified = [value for value in checks.values() if value is not None]
    passed = all(specified) if specified else True
    return {"id": case["id"], "type": "receipt_review", "passed": passed, "checks": checks, "actual": actual}


def _evaluate_chat_case(db, case: dict[str, Any]) -> dict[str, Any]:
    scope = case["type"].replace("_chat", "")
    submission_id = None
    if scope == "case":
        submission = _resolve_submission(db, case)
        analyze_submission(db, submission.id)
        submission_id = submission.id

    response = answer_chat_question(db, scope, case["question"], submission_id)
    expected = case.get("expected", {})
    citations = _serialize_citations(response.get("citations"))
    actual = {
        "scope": response["scope"],
        "grounded": response["grounded"],
        "answer": response["answer"],
        "citations": citations,
        "confidence": response.get("confidence", {}),
    }
    checks = {
        "grounded": None if "grounded" not in expected else (actual["grounded"] == expected["grounded"]),
        "retrieval_quality": _citations_match(citations, expected),
        "citation_correctness": _citations_match(citations, expected),
        "answer_relevance": _answer_relevance_match(actual["answer"], expected),
        "refusal_behavior": _refusal_match(actual["answer"], actual["grounded"], expected),
    }
    specified = [value for value in checks.values() if value is not None]
    passed = all(specified) if specified else True
    return {"id": case["id"], "type": case["type"], "passed": passed, "checks": checks, "actual": actual}


def run_suite(spec: dict[str, Any]) -> dict[str, Any]:
    verdict_accuracy = MetricBucket()
    category_accuracy = MetricBucket()
    retrieval_quality = MetricBucket()
    citation_correctness = MetricBucket()
    answer_relevance = MetricBucket()
    grounded_accuracy = MetricBucket()
    refusal_rate = MetricBucket()
    overall_pass = MetricBucket()

    results: list[dict[str, Any]] = []
    with SessionLocal() as db:
        bootstrap_data(db)
        for case in spec.get("cases", []):
            case_type = case["type"]
            if case_type == "receipt_review":
                result = _evaluate_receipt_case(db, case)
                verdict_accuracy.add(result["checks"].get("verdict"))
                category_accuracy.add(result["checks"].get("category"))
            elif case_type in {"policy_chat", "case_chat"}:
                result = _evaluate_chat_case(db, case)
                grounded_accuracy.add(result["checks"].get("grounded"))
                answer_relevance.add(result["checks"].get("answer_relevance"))
                refusal_rate.add(result["checks"].get("refusal_behavior"))
            else:
                raise ValueError(f"Unsupported case type: {case_type}")

            retrieval_quality.add(result["checks"].get("retrieval_quality"))
            citation_correctness.add(result["checks"].get("citation_correctness"))
            overall_pass.add(result["passed"])
            results.append(result)

    summary = {
        "suite_name": spec.get("suite_name", "expense-review-eval"),
        "metrics": {
            "case_pass_rate": overall_pass.as_dict(),
            "accuracy": verdict_accuracy.as_dict(),
            "category_accuracy": category_accuracy.as_dict(),
            "retrieval_quality": retrieval_quality.as_dict(),
            "citation_correctness": citation_correctness.as_dict(),
            "grounded_accuracy": grounded_accuracy.as_dict(),
            "answer_relevance": answer_relevance.as_dict(),
            "refusal_rate": refusal_rate.as_dict(),
        },
        "results": results,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Northwind expense-review evaluation harness.")
    parser.add_argument("spec", type=Path, help="Path to the JSON evaluation spec.")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to save the JSON report.")
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    report = run_suite(spec)

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2))

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
