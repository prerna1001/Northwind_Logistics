from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:  # pragma: no cover - optional until deps are installed
    RecursiveCharacterTextSplitter = None

from .analysis import embed_text
from .config import get_settings
from .database import Base, engine, ensure_vector_extension
from .extraction import extract_pdf_text
from .migrations import ensure_compatibility_columns
from .models import (
    DeterministicFinding,
    Employee,
    PolicyChunk,
    PolicyDocument,
    PolicyRule,
    Receipt,
    ReceiptExtraction,
    ReviewOverride,
    Submission,
    Verdict,
)
from .storage import store_file, store_json


settings = get_settings()


RULE_BLUEPRINTS = [
    {
        "rule_id": "meal_breakfast_cap",
        "document_code": "TEP-002",
        "rule_type": "amount_cap",
        "category": "meal",
        "params": {"meal_type": "breakfast", "max_amount": 25.0},
        "severity_hint": "flagged",
        "phrase": "Breakfast: $25",
    },
    {
        "rule_id": "meal_lunch_cap",
        "document_code": "TEP-002",
        "rule_type": "amount_cap",
        "category": "meal",
        "params": {"meal_type": "lunch", "max_amount": 35.0},
        "severity_hint": "flagged",
        "phrase": "Lunch: $35",
    },
    {
        "rule_id": "meal_dinner_cap",
        "document_code": "TEP-002",
        "rule_type": "amount_cap",
        "category": "meal",
        "params": {"meal_type": "dinner", "max_amount": 75.0},
        "severity_hint": "flagged",
        "phrase": "Dinner: $75",
    },
    {
        "rule_id": "solo_travel_alcohol",
        "document_code": "TEP-003",
        "rule_type": "prohibited_item",
        "category": "meal",
        "params": {"signal": "contains_alcohol", "when": "solo_travel"},
        "severity_hint": "rejected",
        "phrase": "Any alcoholic beverage purchased while traveling on business without external clients present is not reimbursable",
    },
    {
        "rule_id": "conference_included_meals",
        "document_code": "TEP-014",
        "rule_type": "included_meal_conflict",
        "category": "meal",
        "params": {"source_receipt_category": "conference"},
        "severity_hint": "flagged",
        "phrase": "Meals included with conference registration are not separately reimbursable",
    },
    {
        "rule_id": "receipt_itemization_required",
        "document_code": "TEP-007",
        "rule_type": "required_itemization",
        "category": "meal",
        "params": {"minimum_line_items": 1},
        "severity_hint": "flagged",
        "phrase": "Meal and entertainment expenses require an original itemized receipt",
    },
    {
        "rule_id": "amount_mismatch",
        "document_code": "TEP-007",
        "rule_type": "amount_mismatch",
        "category": "other",
        "params": {"tolerance": 0.75},
        "severity_hint": "flagged",
        "phrase": "If the claimed amount differs from the receipt amount, Finance reimburses the lesser amount unless the discrepancy is justified",
    },
    {
        "rule_id": "air_travel_first_class",
        "document_code": "TEP-005",
        "rule_type": "prohibited_item",
        "category": "air_travel",
        "params": {"class_contains": "First"},
        "severity_hint": "rejected",
        "phrase": "First class is not reimbursable under any circumstances",
    },
    {
        "rule_id": "premium_economy_duration",
        "document_code": "TEP-005",
        "rule_type": "approval_required",
        "category": "air_travel",
        "params": {"class_contains": "Premium", "minimum_duration_hours": 6.0},
        "severity_hint": "flagged",
        "phrase": "Premium economy is permitted on any single flight segment with a scheduled duration of 6 hours or more",
    },
    {
        "rule_id": "international_requires_vp_approval",
        "document_code": "TEP-013",
        "rule_type": "approval_required",
        "category": "other",
        "params": {"requires_signal": "international_trip"},
        "severity_hint": "flagged",
        "phrase": "All international travel requires advance written approval from a Vice President",
    },
    {
        "rule_id": "corporate_card_expected",
        "document_code": "TEP-010",
        "rule_type": "required_receipt_field",
        "category": "other",
        "params": {"field": "payment_method", "contains": "Corporate", "requires_signal": "possible_mismatch"},
        "severity_hint": "flagged",
        "phrase": "The Corporate Card should be the primary payment method for business travel and entertainment expenses",
    },
]


def bootstrap_database() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_store_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    ensure_vector_extension()
    Base.metadata.create_all(bind=engine)
    ensure_compatibility_columns(engine)


def _extract_metadata(block: str) -> dict[str, str | None]:
    def match(pattern: str) -> str | None:
        found = re.search(pattern, block)
        return found.group(1).strip() if found else None

    title_line = block.splitlines()[0].strip()
    return {
        "title": title_line,
        "document_code": match(r"Document:\s*([A-Z-0-9]+)"),
        "version": match(r"Version:\s*([0-9.]+)"),
        "effective_date": match(r"Effective Date:\s*([A-Za-z0-9,\s]+?)(?:Owner:|$)"),
        "owner": match(r"Owner:\s*(.+?)(?:Applies To:|$)"),
    }


def _split_policy_bundle(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    starts = list(
        re.finditer(
            r"(?m)^\f?(?P<title>[^\n\f][^\n]+)\nDocument:\s*[A-Z]{3}-\d+",
            normalized,
        )
    )
    segments: list[str] = []
    for index, match in enumerate(starts):
        start = match.start()
        end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
        segments.append(normalized[start:end].strip())
    return segments


def _policy_family_for(title: str) -> str:
    return title.strip()


def _topic_tags(text: str) -> list[str]:
    keywords = {
        "meal": ["meal", "breakfast", "lunch", "dinner", "restaurant"],
        "alcohol": ["alcohol", "beer", "wine", "cocktail"],
        "lodging": ["lodging", "hotel", "room", "marriott", "hyatt", "hilton"],
        "air_travel": ["air travel", "flight", "premium economy", "first class", "business class"],
        "ground_transport": ["ground transportation", "uber", "lyft", "rideshare", "taxi"],
        "receipt_itemization": ["itemized receipt", "receipt", "line item"],
        "conference": ["conference", "registration", "included meals", "attendee"],
        "corporate_card": ["corporate card", "card issuer", "payment method"],
        "approval": ["approval", "director approval", "vp approval", "manager approval"],
        "conduct": ["falsification", "duplicated expense claims", "misrepresentation"],
    }
    lowered = text.lower()
    return [tag for tag, terms in keywords.items() if any(term in lowered for term in terms)]


def _chunk_policy(text: str) -> list[dict]:
    normalized = re.sub(r"\n{3,}", "\n\n", text.replace("\f", "\n"))
    sections = re.split(r"\n(?=\d+(?:\.\d+)*\.)", normalized)
    chunks: list[dict] = []
    splitter = (
        RecursiveCharacterTextSplitter(
            chunk_size=900,
            chunk_overlap=120,
            separators=["\n\n", "\n", ". ", " "],
        )
        if RecursiveCharacterTextSplitter is not None
        else None
    )
    for raw_section in sections:
        section = raw_section.strip()
        if not section:
            continue
        first_line = section.splitlines()[0]
        match = re.match(r"(?P<section>\d+(?:\.\d+)*\.)\s*(?P<title>.+)", first_line)
        section_key = match.group("section") if match else None
        parts = splitter.split_text(section) if splitter and len(section) > 1100 else [section]
        for index, part in enumerate(parts, start=1):
            chunks.append(
                {
                    "section_key": section_key,
                    "part_index": index,
                    "topic_tags": _topic_tags(part),
                    "chunk_text": part,
                }
            )
    if not chunks:
        chunks.append(
            {
                "section_key": None,
                "part_index": 1,
                "topic_tags": _topic_tags(normalized),
                "chunk_text": normalized,
            }
        )
    return chunks


def _find_quote(document: PolicyDocument, phrase: str) -> tuple[str | None, str]:
    for chunk in document.chunks:
        if phrase.lower() in chunk.chunk_text.lower():
            return chunk.section_key, chunk.chunk_text
    return None, document.raw_text[:600]


def seed_policies(db: Session) -> None:
    existing_bundles = {
        row[0]
        for row in db.execute(select(PolicyDocument.bundle_name)).all()
    }
    for bundle in sorted(settings.policies_dir.glob("*.pdf")):
        storage = store_file(f"policies/raw/{bundle.name}", bundle)
        text = extract_pdf_text(bundle)
        if bundle.name in existing_bundles:
            continue
        for block in _split_policy_bundle(text):
            metadata = _extract_metadata(block)
            if not metadata["document_code"]:
                continue
            document = PolicyDocument(
                bundle_name=bundle.name,
                document_code=metadata["document_code"],
                title=metadata["title"] or bundle.stem,
                policy_family=_policy_family_for(metadata["title"] or bundle.stem),
                version=metadata["version"],
                effective_date=metadata["effective_date"],
                owner=metadata["owner"],
                s3_source_key=storage.s3_key,
                is_active=True,
                raw_text=block,
            )
            db.add(document)
            db.flush()
            for chunk_data in _chunk_policy(block):
                db.add(
                    PolicyChunk(
                        document_id=document.id,
                        section_key=chunk_data["section_key"],
                        part_index=chunk_data["part_index"],
                        topic_tags=chunk_data["topic_tags"],
                        chunk_text=chunk_data["chunk_text"],
                        is_active=True,
                        embedding=embed_text(chunk_data["chunk_text"]),
                    )
                )
        db.commit()


def seed_policy_rules(db: Session) -> None:
    existing_rules = db.scalars(select(PolicyRule)).all()
    if existing_rules:
        expected_rule_ids = {blueprint["rule_id"] for blueprint in RULE_BLUEPRINTS}
        existing_rule_ids = {rule.rule_id for rule in existing_rules}
        corporate_rule = next((rule for rule in existing_rules if rule.rule_id == "corporate_card_expected"), None)
        corporate_requires_signal = None
        if corporate_rule is not None and isinstance(corporate_rule.params, dict):
            corporate_requires_signal = corporate_rule.params.get("requires_signal")
        if existing_rule_ids == expected_rule_ids and corporate_requires_signal == "possible_mismatch":
            return
        db.execute(delete(PolicyRule))
        db.commit()

    documents = {doc.document_code: doc for doc in db.scalars(select(PolicyDocument)).all()}
    for blueprint in RULE_BLUEPRINTS:
        document = documents.get(blueprint["document_code"])
        if document is None:
            continue
        section_key, quote = _find_quote(document, blueprint["phrase"])
        db.add(
            PolicyRule(
                rule_id=blueprint["rule_id"],
                document_id=document.id,
                section_key=section_key,
                rule_type=blueprint["rule_type"],
                category=blueprint["category"],
                params=blueprint["params"],
                severity_hint=blueprint["severity_hint"],
                quoted_source_text=quote,
                is_active=True,
                version=document.version,
            )
        )
    db.commit()


def seed_sample_cases(db: Session) -> None:
    for sample_dir in sorted(settings.sample_submissions_dir.glob("*")):
        if not sample_dir.is_dir():
            continue
        json_path = sample_dir / "employee_info.json"
        receipts_dir = sample_dir / "receipts"
        if not json_path.exists() or not receipts_dir.exists():
            continue
        employee_payload = json.loads(json_path.read_text())
        employee = db.get(Employee, employee_payload["employee_id"])
        if employee is None:
            employee = Employee(**employee_payload, source="sample")
            db.add(employee)
            db.flush()
        else:
            for key, value in employee_payload.items():
                setattr(employee, key, value)
            employee.source = "sample"

        employee_storage = store_json(
            f"cases/sample/{sample_dir.name}/employee_info.json",
            employee_payload,
        )

        submission = db.scalars(
            select(Submission).where(Submission.sample_case_id == sample_dir.name)
        ).first()
        if submission is None:
            submission = Submission(
                employee_id=employee.employee_id,
                source="sample",
                sample_case_id=sample_dir.name,
                employee_info_s3_key=employee_storage.s3_key,
                trip_purpose=employee_payload.get("trip_purpose"),
                trip_dates=employee_payload.get("trip_dates"),
                status="seeded_sample",
            )
            db.add(submission)
            db.flush()
        else:
            submission.employee_info_s3_key = employee_storage.s3_key

        existing = {
            row[0]
            for row in db.execute(
                select(Receipt.original_filename).where(Receipt.submission_id == submission.id)
            ).all()
        }
        for receipt_path in sorted(receipts_dir.glob("*")):
            if not receipt_path.is_file() or receipt_path.name.startswith(".") or receipt_path.name in existing:
                continue
            stored = store_file(
                f"cases/sample/{sample_dir.name}/receipts/{receipt_path.name}",
                receipt_path,
            )
            db.add(
                Receipt(
                    submission_id=submission.id,
                    original_filename=receipt_path.name,
                    file_type=receipt_path.suffix.lower().lstrip(".") or "unknown",
                    mime_type=None,
                    source="sample",
                    storage_backend=stored.backend,
                    s3_key=stored.s3_key,
                    storage_uri=stored.uri,
                )
            )
        db.commit()


def _legacy_seed_detected(db: Session) -> bool:
    bundle_count = len(list(settings.policies_dir.glob("*.pdf")))
    policy_document_count = db.scalar(select(func.count()).select_from(PolicyDocument)) or 0
    sample_submission_count = db.scalar(
        select(func.count()).select_from(Submission).where(Submission.source == "sample")
    ) or 0
    sample_receipt_count = db.scalar(
        select(func.count())
        .select_from(Receipt)
        .join(Submission, Receipt.submission_id == Submission.id)
        .where(Submission.source == "sample")
    ) or 0
    policy_rule_count = db.scalar(select(func.count()).select_from(PolicyRule)) or 0

    if policy_document_count and policy_document_count <= bundle_count:
        return True
    if sample_submission_count and sample_receipt_count == 0:
        return True
    if policy_document_count and policy_rule_count == 0:
        return True
    documents = db.scalars(select(PolicyDocument).limit(50)).all()
    for document in documents:
        if (document.raw_text or "").count("Document:") > 1:
            return True
    return False


def _reset_legacy_seed_data(db: Session) -> None:
    sample_receipt_ids = (
        select(Receipt.id)
        .join(Submission, Receipt.submission_id == Submission.id)
        .where(Submission.source == "sample")
    )
    sample_verdict_ids = select(Verdict.id).where(Verdict.receipt_id.in_(sample_receipt_ids))

    db.execute(delete(ReviewOverride).where(ReviewOverride.verdict_id.in_(sample_verdict_ids)))
    db.execute(delete(Verdict).where(Verdict.receipt_id.in_(sample_receipt_ids)))
    db.execute(delete(DeterministicFinding).where(DeterministicFinding.receipt_id.in_(sample_receipt_ids)))
    db.execute(delete(ReceiptExtraction).where(ReceiptExtraction.receipt_id.in_(sample_receipt_ids)))
    db.execute(delete(Receipt).where(Receipt.id.in_(sample_receipt_ids)))
    db.execute(delete(Submission).where(Submission.source == "sample"))

    db.execute(delete(PolicyRule))
    db.execute(delete(PolicyChunk))
    db.execute(delete(PolicyDocument))
    db.commit()


def bootstrap_data(db: Session) -> None:
    bootstrap_database()
    if _legacy_seed_detected(db):
        print("Detected legacy seeded data. Resetting policies and sample cases for the current build.")
        _reset_legacy_seed_data(db)
    seed_policies(db)
    seed_policy_rules(db)
    seed_sample_cases(db)
