from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime

import httpx
from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .extraction import compute_extraction_confidence, detect_file_type, extract_text, looks_like_receipt, normalize_receipt_text
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
from .storage import _local_path_for_key


settings = get_settings()


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "tolist") and callable(value.tolist):
        try:
            return _json_safe(value.tolist())
        except Exception:
            pass
    return value


def embed_text(text: str) -> list[float]:
    vector = [0.0] * settings.embedding_dimensions
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % settings.embedding_dimensions
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def cosine_similarity(first: list[float] | None, second: list[float] | None) -> float:
    if first is None or second is None:
        return 0.0
    if len(first) == 0 or len(second) == 0:
        return 0.0
    numerator = sum(left * right for left, right in zip(first, second))
    left_norm = math.sqrt(sum(value * value for value in first)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in second)) or 1.0
    return float(max(0.0, min(1.0, numerator / (left_norm * right_norm))))


def _keyword_overlap(query: str, target: str) -> float:
    query_tokens = {token for token in re.findall(r"[a-z0-9]+", query.lower()) if len(token) > 2}
    target_tokens = {token for token in re.findall(r"[a-z0-9]+", target.lower()) if len(token) > 2}
    if not query_tokens or not target_tokens:
        return 0.0
    return len(query_tokens & target_tokens) / max(len(query_tokens), 1)


def _text_for_receipt(receipt: Receipt) -> str:
    local_path = _local_path_for_key(receipt.s3_key)
    file_type = detect_file_type(receipt.original_filename, receipt.mime_type)
    text, status = extract_text(local_path, file_type)
    normalized = normalize_receipt_text(text, receipt.original_filename) if text else {
        "vendor": None,
        "date": None,
        "meal_type": None,
        "category": "other",
        "subtotal": None,
        "tax": None,
        "tip": None,
        "total": None,
        "line_items": [],
        "payment_method": None,
        "notes": [],
        "attendees": [],
        "contains_alcohol": False,
        "flight_duration_hours": None,
        "class_of_service": None,
    }
    if status == "completed" and not looks_like_receipt(normalized):
        status = "needs_human_review"
    confidence = compute_extraction_confidence(text, normalized, status)
    if receipt.extraction is None:
        receipt.extraction = ReceiptExtraction(
            receipt_id=receipt.id,
            parser_version=settings.receipt_parser_version,
        )
    receipt.extraction.parser_version = settings.receipt_parser_version
    receipt.extraction.extraction_status = status
    receipt.extraction.extraction_confidence = confidence
    receipt.extraction.raw_text = text
    receipt.extraction.normalized_data = _json_safe(normalized)
    receipt.extraction.category = normalized["category"]
    receipt.extraction.needs_retry = status == "needs_human_review"
    return text


def _build_policy_family_profiles(db: Session) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    documents = db.scalars(
        select(PolicyDocument)
        .where(PolicyDocument.is_active.is_(True))
        .options(selectinload(PolicyDocument.chunks))
    ).all()
    for document in documents:
        family = document.policy_family or document.title or document.document_code
        text = " ".join(chunk.chunk_text[:300] for chunk in document.chunks[:3])
        tags = sorted({tag for chunk in document.chunks for tag in (chunk.topic_tags or [])})
        summary = f"{document.title}. {' '.join(tags)}. {text}"
        profiles[family] = {
            "document_id": document.id,
            "title": document.title,
            "summary": summary,
            "tags": tags,
            "embedding": embed_text(summary),
        }
    return profiles


def _trip_signal(submission: Submission, employee: Employee, extraction: ReceiptExtraction) -> dict:
    notes_text = " ".join(extraction.normalized_data.get("notes", []))
    total_attendees = extraction.normalized_data.get("attendees", [])
    trip_text = f"{submission.trip_purpose or ''} {employee.trip_purpose or ''}".lower()
    payment_method = extraction.normalized_data.get("payment_method") or ""
    international = any(keyword in trip_text for keyword in ["canada", "mexico", "international", "europe", "asia"])
    company_card = "corporate" in payment_method.lower()
    possible_mismatch = "outside concur" in extraction.raw_text.lower() if extraction.raw_text else False
    return {
        "contains_alcohol": bool(extraction.normalized_data.get("contains_alcohol")),
        "conference_related": "conference" in trip_text or extraction.category == "conference",
        "international_trip": international,
        "company_card_used": company_card,
        "possible_mismatch": possible_mismatch,
        "solo_travel": "solo" in trip_text or ("guest 1" in (extraction.raw_text or "").lower() and not total_attendees),
        "has_external_attendees": any("external" in attendee.lower() for attendee in total_attendees),
        "payment_method": payment_method,
        "notes_text": notes_text,
    }


def _seed_policy_families(category: str) -> list[str]:
    mapping = {
        "meal": ["Meals and Entertainment Policy", "Receipt Requirements Policy", "Alcohol Policy"],
        "lodging": ["Lodging Policy", "Receipt Requirements Policy"],
        "air_travel": ["Air Travel Policy", "Receipt Requirements Policy"],
        "ground_transport": ["Ground Transportation Policy", "Receipt Requirements Policy"],
        "conference": ["Conference Attendance Policy", "Receipt Requirements Policy", "Meals and Entertainment Policy"],
    }
    return mapping.get(category, ["Travel & Expense Policy — Overview", "Receipt Requirements Policy"])


def _supporting_policy_candidates(signals: dict) -> set[str]:
    families: set[str] = set()
    if signals.get("contains_alcohol"):
        families.add("Alcohol Policy")
    if signals.get("conference_related"):
        families.add("Conference Attendance Policy")
    if signals.get("international_trip"):
        families.add("International Travel Policy")
    if signals.get("possible_mismatch"):
        families.update({"Code of Conduct", "Corporate Card Policy"})
    return families


def _route_policies(extraction: ReceiptExtraction, submission: Submission, employee: Employee, profiles: dict[str, dict]) -> dict:
    category = extraction.category or extraction.normalized_data.get("category") or "other"
    signals = _trip_signal(submission, employee, extraction)
    receipt_like = looks_like_receipt(extraction.normalized_data)
    category_recognized = category != "other"
    base_families = _seed_policy_families(category) if receipt_like and category_recognized else []
    supporting_candidates = _supporting_policy_candidates(signals)
    query_summary = " ".join(
        filter(
            None,
            [
                category,
                extraction.normalized_data.get("vendor"),
                extraction.normalized_data.get("meal_type"),
                "alcohol" if signals["contains_alcohol"] else None,
                "conference" if signals["conference_related"] else None,
                "possible mismatch" if signals["possible_mismatch"] else None,
                submission.trip_purpose,
                employee.home_base,
                " ".join(item["description"] for item in extraction.normalized_data.get("line_items", [])[:6]),
            ],
        )
    )
    query_embedding = embed_text(query_summary)
    scored_families: list[dict] = []
    if receipt_like and category_recognized:
        for family, profile in profiles.items():
            if family not in base_families and family not in supporting_candidates:
                continue
            vector_score = cosine_similarity(query_embedding, profile["embedding"])
            keyword_score = _keyword_overlap(query_summary, profile["summary"])
            tag_overlap = len(set(profile["tags"]) & set(category.split("_"))) * 0.08
            fuzzy = fuzz.partial_ratio(query_summary.lower(), profile["summary"].lower()) / 100.0
            score = (0.5 * vector_score) + (0.22 * keyword_score) + (0.18 * fuzzy) + tag_overlap
            if signals["contains_alcohol"] and "Alcohol Policy" == family:
                score += 0.25
            if signals["possible_mismatch"] and "Code of Conduct" == family:
                score += 0.12
            if signals["possible_mismatch"] and "Corporate Card Policy" == family:
                score += 0.12
            if signals["conference_related"] and "Conference Attendance Policy" == family:
                score += 0.16
            if signals["international_trip"] and "International Travel Policy" == family:
                score += 0.2
            if family in base_families:
                score += 0.2
            scored_families.append({"family": family, "score": float(round(score, 3))})
    supporting = [item["family"] for item in sorted(scored_families, key=lambda row: row["score"], reverse=True) if item["score"] >= 0.48][:5]
    families = list(dict.fromkeys(base_families + supporting))
    routing = {
        "primary_category": category,
        "category_recognized": category_recognized,
        "receipt_like": receipt_like,
        "secondary_signals": signals,
        "query_summary": query_summary,
        "seed_policy_families": base_families,
        "routed_policy_families": families,
        "family_scores": sorted(scored_families, key=lambda row: row["score"], reverse=True)[:8],
    }
    extraction.routing_data = _json_safe(routing)
    return routing


def _conference_receipt_text(receipts: list[Receipt]) -> str:
    for receipt in receipts:
        if receipt.extraction and receipt.extraction.category == "conference":
            return receipt.extraction.raw_text or ""
    return ""


def _evaluate_rule(rule: PolicyRule, extraction: ReceiptExtraction, submission: Submission, employee: Employee, routing: dict, all_receipts: list[Receipt]) -> dict | None:
    data = extraction.normalized_data
    signals = routing["secondary_signals"]
    params = rule.params
    category = routing["primary_category"]
    if rule.category not in {category, "other"}:
        return None
    triggered = False
    facts: dict = {}
    summary = ""

    if rule.rule_type == "amount_cap" and category == "meal":
        if data.get("meal_type") == params.get("meal_type") and data.get("total") is not None:
            if float(data["total"]) > float(params["max_amount"]):
                triggered = True
                facts = {"meal_type": data.get("meal_type"), "total": data.get("total"), "max_amount": params["max_amount"]}
                summary = f"{data.get('meal_type', 'Meal').title()} total ${data.get('total'):.2f} exceeds the ${params['max_amount']:.2f} cap."
    elif rule.rule_type == "prohibited_item":
        if params.get("signal") == "contains_alcohol" and signals["contains_alcohol"] and signals["solo_travel"] and not signals["has_external_attendees"]:
            triggered = True
            facts = {"contains_alcohol": True, "solo_travel": True, "has_external_attendees": False}
            summary = "Alcohol appears on a solo-travel meal receipt with no external attendees."
        elif params.get("class_contains") and params["class_contains"].lower() in (data.get("class_of_service") or "").lower():
            triggered = True
            facts = {"class_of_service": data.get("class_of_service")}
            summary = f"Class of service '{data.get('class_of_service')}' is not reimbursable under the air-travel policy."
    elif rule.rule_type == "included_meal_conflict" and category == "meal" and signals["conference_related"]:
        registration_text = _conference_receipt_text(all_receipts).lower()
        meal_type = data.get("meal_type")
        if meal_type and meal_type in registration_text:
            triggered = True
            facts = {"meal_type": meal_type, "conference_registration_mentions": meal_type}
            summary = f"This {meal_type} may already be included in the conference registration."
    elif rule.rule_type == "required_itemization" and category == "meal":
        if len(data.get("line_items", [])) < int(params.get("minimum_line_items", 1)):
            triggered = True
            facts = {"line_item_count": len(data.get("line_items", []))}
            summary = "Meal receipt does not appear itemized enough for policy requirements."
    elif rule.rule_type == "amount_mismatch":
        if signals["possible_mismatch"]:
            triggered = True
            facts = {"reason": "outside_concur_or_adjustment_note"}
            summary = "Receipt includes an anomaly note that could require amount review."
    elif rule.rule_type == "approval_required":
        if params.get("class_contains"):
            class_name = data.get("class_of_service") or ""
            duration = data.get("flight_duration_hours") or 0
            minimum = float(params.get("minimum_duration_hours", 0))
            if params["class_contains"].lower() in class_name.lower() and duration < minimum:
                triggered = True
                facts = {"class_of_service": class_name, "duration_hours": duration, "minimum_duration_hours": minimum}
                summary = f"{class_name} appears on a segment below the {minimum:.1f}-hour threshold."
        elif params.get("requires_signal") == "international_trip" and signals["international_trip"]:
            triggered = True
            facts = {"international_trip": True}
            summary = "International travel requires VP approval."
    elif rule.rule_type == "required_receipt_field":
        field_name = params.get("field")
        field_value = data.get(field_name) or signals.get(field_name)
        expected = str(params.get("contains", "")).lower()
        required_signal = params.get("requires_signal")
        if required_signal and not signals.get(required_signal):
            return None
        if field_name == "payment_method" and not field_value:
            return None
        if not field_value or expected not in str(field_value).lower():
            triggered = True
            facts = {"field": field_name, "value": field_value}
            summary = "Expected business travel card information is missing or does not look corporate."

    if not triggered:
        return None
    return {
        "rule_id": rule.rule_id,
        "triggered": True,
        "severity_hint": rule.severity_hint,
        "matched_facts": _json_safe(facts),
        "summary": summary,
        "policy_reference": _json_safe({
            "document_code": rule.document.document_code,
            "policy_title": rule.document.title,
            "section_key": rule.section_key,
            "quote": rule.quoted_source_text[:500],
        }),
    }


def _run_deterministic_checks(db: Session, receipt: Receipt, submission: Submission, routing: dict) -> list[dict]:
    db.execute(delete(DeterministicFinding).where(DeterministicFinding.receipt_id == receipt.id))
    rules = db.scalars(
        select(PolicyRule)
        .where(PolicyRule.is_active.is_(True))
        .options(selectinload(PolicyRule.document))
    ).all()
    findings: list[dict] = []
    for rule in rules:
        finding = _evaluate_rule(rule, receipt.extraction, submission, submission.employee, routing, submission.receipts)
        if finding:
            findings.append(finding)
            db.add(
                DeterministicFinding(
                    receipt_id=receipt.id,
                    rule_id=finding["rule_id"],
                    triggered=True,
                    severity_hint=finding["severity_hint"],
                    matched_facts=_json_safe(finding["matched_facts"]),
                    summary=finding["summary"],
                    policy_reference=_json_safe(finding["policy_reference"]),
                )
            )
    db.flush()
    return findings


def _retrieve_evidence(
    db: Session,
    routing: dict,
    findings: list[dict],
    extraction_confidence: float,
    extraction_status: str | None,
) -> tuple[list[dict], float, list[str]]:
    if extraction_status in {"unsupported", "needs_human_review"} or extraction_confidence < 0.35:
        return [], 0.0, ["Receipt extraction was too weak to support reliable policy retrieval."]
    if not routing.get("receipt_like", False):
        return [], 0.0, ["This file does not look enough like a receipt to support policy retrieval."]
    if not routing.get("category_recognized", False) or not routing.get("routed_policy_families"):
        return [], 0.0, ["The receipt could not be categorized confidently enough to retrieve policy evidence."]
    query_summary = routing["query_summary"]
    query_embedding = embed_text(query_summary)
    family_set = set(routing["routed_policy_families"])
    primary_category = routing["primary_category"]
    preferred_family_map = {
        "meal": {"Meals and Entertainment Policy", "Alcohol Policy", "Receipt Requirements Policy"},
        "lodging": {"Lodging Policy", "Receipt Requirements Policy"},
        "air_travel": {"Air Travel Policy", "Receipt Requirements Policy"},
        "ground_transport": {"Ground Transportation Policy", "Receipt Requirements Policy"},
        "conference": {"Conference Attendance Policy", "Receipt Requirements Policy", "Meals and Entertainment Policy"},
    }
    preferred_families = preferred_family_map.get(primary_category, set())
    chunks = db.scalars(
        select(PolicyChunk)
        .join(PolicyChunk.document)
        .where(PolicyChunk.is_active.is_(True), PolicyDocument.is_active.is_(True))
        .options(selectinload(PolicyChunk.document))
    ).all()
    scored = []
    finding_terms = " ".join(finding["summary"] for finding in findings)
    for chunk in chunks:
        if chunk.document is None:
            continue
        document_family = chunk.document.policy_family or chunk.document.title or chunk.document.document_code
        if document_family not in family_set:
            continue
        vector_score = cosine_similarity(query_embedding, chunk.embedding)
        keyword_score = _keyword_overlap(query_summary + " " + finding_terms, chunk.chunk_text)
        tag_score = len(set(chunk.topic_tags or []) & set(routing["primary_category"].split("_"))) * 0.06
        family_boost = 0.1 if document_family in routing["seed_policy_families"] else 0.0
        score = (0.55 * vector_score) + (0.25 * keyword_score) + tag_score + family_boost
        if preferred_families and document_family in preferred_families:
            score += 0.18
        if primary_category not in {"other", "policy_question", "case_question"} and document_family == "Travel & Expense Policy — Overview":
            score -= 0.18
        topic_tags = set(chunk.topic_tags or [])
        if primary_category in topic_tags:
            score += 0.1
        elif topic_tags and primary_category not in {"other", "policy_question", "case_question"}:
            score -= 0.05
        scored.append(
            {
                "chunk_id": chunk.id,
                "policy_title": chunk.document.title,
                "document_code": chunk.document.document_code,
                "policy_family": document_family,
                "section_key": chunk.section_key,
                "chunk_text": chunk.chunk_text,
                "topic_tags": chunk.topic_tags or [],
                "score": float(round(score, 3)),
                "match_reason": "primary" if document_family in routing["seed_policy_families"] else "supporting",
            }
        )
    evidence = sorted(scored, key=lambda item: item["score"], reverse=True)[:5]
    top_scores = [item["score"] for item in evidence]
    raw_average = (sum(top_scores) / max(len(top_scores), 1)) if top_scores else 0.0
    primary_bonus = 0.05 if evidence and evidence[0]["match_reason"] == "primary" else 0.0
    retrieval_confidence = float(round(min(0.98, raw_average * 1.25 + 0.1 + primary_bonus), 2))
    reasons = []
    if evidence:
        reasons.append(f"Retrieved {len(evidence)} policy chunks from routed policy families.")
        if evidence[0]["score"] >= 0.6:
            reasons.append("Top policy evidence matched the receipt context strongly.")
        elif evidence[0]["score"] < 0.35:
            reasons.append("Retrieved policy evidence was weak or generic.")
    else:
        reasons.append("No strong policy chunks were retrieved from the routed families.")
    return evidence, retrieval_confidence, reasons


def _confidence_band(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _money_text(value) -> str | None:
    if value is None:
        return None
    try:
        return f"${float(value):.2f}"
    except Exception:
        return str(value)


def _build_reasoning_summary(
    receipt: Receipt,
    routing: dict,
    findings: list[dict],
    evidence: list[dict],
    retrieval_confidence: float,
    verdict: str,
    base_reasoning: str,
) -> str:
    normalized = receipt.extraction.normalized_data
    facts: list[str] = []
    vendor = normalized.get("vendor")
    if vendor:
        facts.append(f"Vendor: {vendor}.")
    if normalized.get("date"):
        facts.append(f"Receipt date: {normalized['date']}.")
    if normalized.get("total") is not None:
        facts.append(f"Total: {_money_text(normalized.get('total'))}.")
    if normalized.get("payment_method"):
        facts.append(f"Payment method: {normalized['payment_method']}.")

    category_line = f"Detected category: {routing.get('primary_category', 'other').replace('_', ' ')}."
    signal_bits: list[str] = []
    signals = routing.get("secondary_signals", {})
    if signals.get("contains_alcohol"):
        signal_bits.append("alcohol was detected")
    if signals.get("conference_related"):
        signal_bits.append("conference context was detected")
    if signals.get("international_trip"):
        signal_bits.append("the trip appears international")
    if signals.get("company_card_used"):
        signal_bits.append("a corporate card appears to have been used")
    if signals.get("possible_mismatch"):
        signal_bits.append("an amount anomaly signal was detected")
    if signals.get("solo_travel"):
        signal_bits.append("the trip appears to be solo travel")
    if signal_bits:
        category_line += " Key signals: " + "; ".join(signal_bits) + "."

    finding_lines: list[str] = []
    if findings:
        for finding in findings[:2]:
            reference = finding.get("policy_reference", {})
            policy_label = reference.get("policy_title") or reference.get("document_code") or "policy"
            section = reference.get("section_key")
            finding_lines.append(
                f"Rule match: {finding['summary']} Supported by {policy_label}{f' {section}' if section else ''}."
            )
    else:
        finding_lines.append("No deterministic rule triggered for this receipt.")

    evidence_lines: list[str] = []
    if evidence:
        top = evidence[:2]
        evidence_lines.append(
            "Retrieved policy support: "
            + " | ".join(
                f"{item['policy_title']}{f' {item['section_key']}' if item['section_key'] else ''} (score {item['score']:.2f})"
                for item in top
            )
            + "."
        )
    else:
        evidence_lines.append("No strong policy clause was retrieved for this receipt.")
    evidence_lines.append(f"Retrieval confidence: {int(round(retrieval_confidence * 100))}%.")

    verdict_lines = [f"Decision: {verdict}.", base_reasoning]
    return "\n\n".join([
        " ".join(facts) if facts else "Receipt facts were only partially extracted.",
        category_line,
        " ".join(finding_lines),
        " ".join(evidence_lines),
        " ".join(verdict_lines),
    ])


def _llama_json(prompt: str) -> dict | None:
    endpoint = settings.resolved_llama_api_url
    if not endpoint:
        return None
    try:
        with httpx.Client(timeout=settings.llama_timeout_seconds) as client:
            headers = {}
            if settings.llama_api_token:
                headers["Authorization"] = f"Bearer {settings.llama_api_token}"
            response = client.post(
                endpoint,
                headers=headers,
                json={
                    "model": settings.llama_model,
                    "messages": [
                        {"role": "system", "content": "Return strict JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
            content = payload.get("choices", [{}])[0].get("message", {}).get("content") or payload.get("message", {}).get("content")
            if not content:
                return None
            return json.loads(content)
    except Exception:
        return None


def _heuristic_adjudication(receipt: Receipt, routing: dict, findings: list[dict], evidence: list[dict], retrieval_confidence: float) -> dict:
    extraction_confidence = receipt.extraction.extraction_confidence
    reasons: list[str] = []
    if extraction_confidence < 0.5:
        reasons.append("Receipt extraction was incomplete.")
    reasons.extend([
        "Policy evidence supports a clear answer." if retrieval_confidence >= 0.7 else "Policy evidence is only moderately strong."
    ])
    strongest = findings[0] if findings else None
    has_rule_evidence = strongest is not None
    decision_score = retrieval_confidence
    verdict = "compliant"
    human_review_needed = False
    reasoning = "No clear policy issue was detected from the receipt facts and retrieved policy evidence."
    recommended_action = "Keep this receipt as compliant unless the reviewer spots additional context."
    policy_findings = []

    if strongest:
        policy_findings.append(strongest["policy_reference"])
        decision_score = min(0.96, max(retrieval_confidence, extraction_confidence, 0.62) + 0.08)
        reasoning = strongest["summary"]
        if strongest["severity_hint"] == "rejected":
            verdict = "rejected"
            recommended_action = "Reject the non-reimbursable portion unless a written exception exists."
        else:
            verdict = "flagged"
            recommended_action = "Flag this receipt for reviewer confirmation or partial reimbursement handling."
    elif evidence:
        policy_findings.extend(
            {
                "document_code": item["document_code"],
                "policy_title": item["policy_title"],
                "section_key": item["section_key"],
                "quote": item["chunk_text"][:500],
            }
            for item in evidence[:2]
        )

    overall = round(min(extraction_confidence, max(retrieval_confidence, 0.01), decision_score), 2)
    if extraction_confidence < 0.45 or retrieval_confidence < 0.4:
        if has_rule_evidence and extraction_confidence >= 0.75:
            verdict = "flagged"
            human_review_needed = False
            overall = round(max(overall, 0.62), 2)
            reasoning = "A policy issue was identified directly from the receipt facts, even though broader retrieval evidence was limited."
            recommended_action = "Flag this receipt for reviewer action based on the matched policy rule."
        else:
            verdict = "needs_human_review"
            human_review_needed = True
            overall = round(min(extraction_confidence, retrieval_confidence, 0.48), 2)
            reasoning = "The system could not gather strong enough evidence to make a reliable call."
            recommended_action = "Send this receipt to a human reviewer."
    elif verdict == "rejected" and overall < 0.72 and not has_rule_evidence:
        verdict = "needs_human_review"
        human_review_needed = True
        reasoning = "There is a likely policy problem, but the evidence is not strong enough for an automatic rejection."
        recommended_action = "Review manually before rejecting."
    elif verdict == "compliant" and (overall < 0.62 or retrieval_confidence < 0.58 or not evidence):
        verdict = "needs_human_review"
        human_review_needed = True
        reasoning = "The receipt looks likely compliant, but the supporting policy evidence is not strong enough for an automatic approval."
        recommended_action = "Review manually before marking this receipt compliant."
    elif verdict == "flagged" and overall < 0.58 and not has_rule_evidence:
        verdict = "needs_human_review"
        human_review_needed = True
        reasoning = "There may be a policy issue, but the evidence is not strong enough for a confident automated flag."
        recommended_action = "Review manually before acting on this flagged result."

    confidence = {
        "extraction": float(extraction_confidence),
        "retrieval": float(retrieval_confidence),
        "decision": float(round(decision_score, 2)),
        "overall": float(overall),
        "band": _confidence_band(overall),
        "reasons": reasons,
    }
    detailed_reasoning = _build_reasoning_summary(
        receipt=receipt,
        routing=routing,
        findings=findings,
        evidence=evidence,
        retrieval_confidence=retrieval_confidence,
        verdict=verdict,
        base_reasoning=reasoning,
    )
    return _json_safe({
        "verdict": verdict,
        "reasoning_summary": detailed_reasoning,
        "confidence": confidence,
        "policy_findings": policy_findings,
        "recommended_action": recommended_action,
        "human_review_needed": human_review_needed,
        "model_name": settings.llama_model if settings.resolved_llama_api_url else "heuristic-adjudicator",
    })


def _adjudicate(receipt: Receipt, submission: Submission, routing: dict, findings: list[dict], evidence: list[dict], retrieval_confidence: float) -> dict:
    prompt_payload = {
        "employee": {
            "employee_id": submission.employee.employee_id,
            "name": submission.employee.name,
            "grade": submission.employee.grade,
            "title": submission.employee.title,
        },
        "trip_context": {
            "trip_purpose": submission.trip_purpose,
            "trip_dates": submission.trip_dates,
        },
        "receipt": _json_safe(receipt.extraction.normalized_data),
        "routing": _json_safe(routing),
        "deterministic_findings": _json_safe(findings),
        "policy_evidence": _json_safe(evidence),
    }
    prompt = (
        "You are reviewing one Northwind expense receipt. "
        "Only answer from the provided policy evidence. "
        "If evidence is weak, set verdict to needs_human_review. "
        "Return JSON with keys verdict, reasoning_summary, policy_findings, recommended_action, human_review_needed. "
        f"\n\nPayload:\n{json.dumps(prompt_payload, indent=2)}"
    )
    model_output = _llama_json(prompt)
    if model_output and isinstance(model_output, dict):
        heuristic = _heuristic_adjudication(receipt, routing, findings, evidence, retrieval_confidence)
        heuristic.update(
            {
                "verdict": model_output.get("verdict", heuristic["verdict"]),
                "reasoning_summary": model_output.get("reasoning_summary", heuristic["reasoning_summary"]),
                "recommended_action": model_output.get("recommended_action", heuristic["recommended_action"]),
                "human_review_needed": bool(model_output.get("human_review_needed", heuristic["human_review_needed"])),
                "policy_findings": model_output.get("policy_findings", heuristic["policy_findings"]),
                "model_name": settings.llama_model,
            }
        )
        if heuristic["confidence"]["overall"] < 0.55:
            heuristic["verdict"] = "needs_human_review"
            heuristic["human_review_needed"] = True
        return heuristic
    return _heuristic_adjudication(receipt, routing, findings, evidence, retrieval_confidence)


def analyze_submission(db: Session, submission_id: int) -> None:
    submission = db.scalars(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.employee),
            selectinload(Submission.receipts).selectinload(Receipt.extraction),
            selectinload(Submission.receipts).selectinload(Receipt.findings),
            selectinload(Submission.receipts).selectinload(Receipt.verdict).selectinload(Verdict.overrides),
        )
    ).first()
    if submission is None:
        raise ValueError("Submission not found")

    profiles = _build_policy_family_profiles(db)
    overall_status = "analyzed"
    for receipt in submission.receipts:
        _text_for_receipt(receipt)
        routing = _route_policies(receipt.extraction, submission, submission.employee, profiles)
        findings = _run_deterministic_checks(db, receipt, submission, routing)
        evidence, retrieval_confidence, retrieval_reasons = _retrieve_evidence(
            db,
            routing,
            findings,
            receipt.extraction.extraction_confidence,
            receipt.extraction.extraction_status,
        )
        adjudication = _adjudicate(receipt, submission, routing, findings, evidence, retrieval_confidence)
        adjudication["confidence"]["reasons"].extend(reason for reason in retrieval_reasons if reason not in adjudication["confidence"]["reasons"])
        if receipt.verdict is None:
            receipt.verdict = Verdict(
                receipt_id=receipt.id,
                verdict=adjudication["verdict"],
                reasoning_summary=adjudication["reasoning_summary"],
                confidence=_json_safe(adjudication["confidence"]),
                policy_findings=_json_safe(adjudication["policy_findings"]),
                recommended_action=adjudication["recommended_action"],
                human_review_needed=adjudication["human_review_needed"],
                model_name=adjudication["model_name"],
            )
        else:
            receipt.verdict.verdict = adjudication["verdict"]
            receipt.verdict.reasoning_summary = adjudication["reasoning_summary"]
            receipt.verdict.confidence = _json_safe(adjudication["confidence"])
            receipt.verdict.policy_findings = _json_safe(adjudication["policy_findings"])
            receipt.verdict.recommended_action = adjudication["recommended_action"]
            receipt.verdict.human_review_needed = adjudication["human_review_needed"]
            receipt.verdict.model_name = adjudication["model_name"]
        if adjudication["verdict"] == "needs_human_review":
            overall_status = "needs_human_review"
    submission.status = overall_status
    submission.last_analysis_at = datetime.utcnow()
    db.commit()


def _compose_policy_answer(question: str, evidence: list[dict], grounded: bool) -> str:
    if not grounded:
        return "I can only answer from the policy library, and I could not find strong enough policy support for that question."
    leading = evidence[0]
    companion = evidence[1] if len(evidence) > 1 else None
    answer = f"{leading['policy_title']} {leading['section_key'] or ''} is the strongest match. {leading['chunk_text'][:340].strip()}"
    if companion:
        answer += f" Supporting policy language also appears in {companion['policy_title']} {companion['section_key'] or ''}."
    return answer


def answer_chat_question(db: Session, scope: str, question: str, submission_id: int | None = None) -> dict:
    profiles = _build_policy_family_profiles(db)
    if scope == "policy":
        routing = {
            "primary_category": "policy_question",
            "secondary_signals": {},
            "query_summary": question,
            "seed_policy_families": list(profiles.keys()),
            "routed_policy_families": list(profiles.keys()),
        }
        evidence, retrieval_confidence, _ = _retrieve_evidence(db, routing, [], 1.0, "completed")
        grounded = bool(evidence and retrieval_confidence >= 0.45)
        citations = [
            {
                "policy_title": item["policy_title"],
                "document_code": item["document_code"],
                "section_key": item["section_key"],
                "quote": item["chunk_text"][:280],
            }
            for item in evidence[:3]
        ]
        return {
            "scope": "policy",
            "grounded": grounded,
            "answer": _compose_policy_answer(question, evidence, grounded),
            "citations": citations,
            "confidence": {
                "retrieval": retrieval_confidence,
                "band": _confidence_band(retrieval_confidence),
            },
        }

    submission = db.scalars(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.employee),
            selectinload(Submission.receipts).selectinload(Receipt.extraction),
            selectinload(Submission.receipts).selectinload(Receipt.findings),
            selectinload(Submission.receipts).selectinload(Receipt.verdict),
        )
    ).first()
    if submission is None:
        raise ValueError("Submission not found")
    summary_lines = [
        f"Employee: {submission.employee.name} ({submission.employee.title})",
        f"Trip: {submission.trip_purpose}",
    ]
    for receipt in submission.receipts:
        if receipt.verdict:
            summary_lines.append(
                f"{receipt.original_filename}: {receipt.verdict.verdict} because {receipt.verdict.reasoning_summary}"
            )
    routing = {
        "primary_category": "case_question",
        "secondary_signals": {},
        "query_summary": question + "\n" + "\n".join(summary_lines),
        "seed_policy_families": list(profiles.keys()),
        "routed_policy_families": list(profiles.keys()),
    }
    evidence, retrieval_confidence, _ = _retrieve_evidence(db, routing, [], 1.0, "completed")
    grounded = bool(evidence)
    answer = (
        f"Case summary for {submission.employee.name}: "
        + " ".join(summary_lines[1:])
        + (" Policy support was also retrieved." if evidence else " No strong supporting policy chunk was retrieved for this question.")
    )
    return {
        "scope": "case",
        "grounded": grounded,
        "answer": answer,
        "citations": [
            {
                "policy_title": item["policy_title"],
                "document_code": item["document_code"],
                "section_key": item["section_key"],
                "quote": item["chunk_text"][:220],
            }
            for item in evidence[:3]
        ],
        "confidence": {
            "retrieval": retrieval_confidence,
            "band": _confidence_band(retrieval_confidence),
        },
    }
