from __future__ import annotations

import mimetypes
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from .config import get_settings
from .storage import StorageResult, store_file


settings = get_settings()

try:
    from paddleocr import PaddleOCR  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    PaddleOCR = None


_OCR_INSTANCE = None


def paddleocr_available() -> bool:
    return PaddleOCR is not None


def get_ocr():
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None and PaddleOCR is not None:
        _OCR_INSTANCE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _OCR_INSTANCE


def detect_file_type(filename: str, mime_type: str | None = None) -> str:
    guessed = (mime_type or mimetypes.guess_type(filename)[0] or "").lower()
    extension = Path(filename).suffix.lower()
    if guessed in {"text/plain", "application/rtf", "text/rtf"} or extension in {".txt", ".rtf"}:
        return "text"
    if guessed == "application/pdf" or extension == ".pdf":
        return "pdf"
    if guessed.startswith("image/") or extension in {".png", ".jpg", ".jpeg", ".webp", ".tiff"}:
        return "image"
    return "unknown"


def extract_pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", str(path), "-"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.stdout.strip():
        return result.stdout.strip()
    return ""


def extract_image_text(path: Path) -> str:
    ocr = get_ocr()
    if ocr is None:
        return ""
    results = ocr.ocr(str(path), cls=True)
    lines: list[str] = []
    for page in results or []:
        for block in page or []:
            if len(block) >= 2 and isinstance(block[1], tuple):
                lines.append(str(block[1][0]).strip())
    return "\n".join(line for line in lines if line)


def _strip_rtf(text: str) -> str:
    # Convert common escaped characters first.
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    # Remove control words like \rtf1, \ansi, \fs24 etc.
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    # Remove braces used for RTF grouping.
    text = text.replace("{", "").replace("}", "")
    # Collapse repeated whitespace while preserving line breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def extract_text(path: Path, file_type: str) -> tuple[str, str]:
    if file_type == "text":
        raw_text = path.read_text(errors="ignore")
        if path.suffix.lower() == ".rtf":
            raw_text = _strip_rtf(raw_text)
        return raw_text, "completed" if raw_text.strip() else "needs_human_review"
    if file_type == "pdf":
        text = extract_pdf_text(path)
        if text:
            return text, "completed"
        image_text = extract_image_text(path)
        return image_text, "completed" if image_text else "needs_human_review"
    if file_type == "image":
        image_text = extract_image_text(path)
        return image_text, "completed" if image_text else "needs_human_review"
    return "", "unsupported"


def _extract_amount(label: str, text: str) -> float | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    label_lower = label.lower()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered == label_lower or lowered.startswith(f"{label_lower} "):
            matches = []
            for follow_up in lines[index : min(index + 8, len(lines))]:
                amount = _money_from_line(follow_up)
                if amount is not None:
                    matches.append(amount)
            if matches:
                return matches[-1]
    pattern = rf"{label}[\s\S]{{0,120}}?(-*\$[0-9,]+\.[0-9]{{2}})"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return _money_from_line(match.group(1))
    return None


def _extract_date(text: str) -> str | None:
    patterns = [
        r"\b\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}(?:\s+\d{1,2}:\d{2}\s+[AP]M)?\b",
        r"\b[A-Z][a-z]{2},\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _money_from_line(line: str) -> float | None:
    stripped = line.strip()
    if stripped.startswith("-$"):
        match = re.search(r"\$([0-9,]+\.[0-9]{2})", stripped)
        return -float(match.group(1).replace(",", "")) if match else None
    stripped = stripped.lstrip("-")
    match = re.search(r"\$([0-9,]+\.[0-9]{2})", stripped)
    return float(match.group(1).replace(",", "")) if match else None


def _last_amount(text: str) -> float | None:
    amounts = [_money_from_line(line) for line in text.splitlines()]
    amounts = [amount for amount in amounts if amount is not None]
    return amounts[-1] if amounts else None


def _extract_time_of_day(text: str) -> str | None:
    if not re.search(r"\d{1,2}:\d{2}\s*[AP]M", text, flags=re.IGNORECASE):
        return None
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", text, flags=re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    meridian = match.group(3).upper()
    if meridian == "PM" and hour != 12:
        hour += 12
    if meridian == "AM" and hour == 12:
        hour = 0
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    return "dinner"


def _clean_vendor(lines: list[str]) -> str | None:
    for line in lines[:8]:
        cleaned = line.strip("= ").strip()
        if not cleaned:
            continue
        if "\\" in cleaned or cleaned.endswith(";"):
            continue
        if cleaned.lower() in {"helvetica", "helvetica;"}:
            continue
        if cleaned.lower().startswith(("record locator", "passenger:", "guest:", "check-in:", "driver:", "attendee:")):
            continue
        if "Document:" in cleaned:
            continue
        if len(cleaned) < 3:
            continue
        return cleaned
    return None


def _extract_payment_method(text: str) -> str | None:
    for pattern in [
        r"Payment:\s*([^\n]+)",
        r"Method:\s*([^\n]+)",
        r"Card:\s*([^\n]+)",
        r"Visa\s+\*{4}\d{4}",
    ]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1) if match.groups() else match.group(0)
            return value.strip()
    return None


def _extract_line_items(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    items: list[dict] = []
    summary_markers = {"subtotal", "tax", "tip", "grand total", "total", "fare", "charges", "daily charges"}
    summary_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lower() in summary_markers or any(line.lower().startswith(f"{marker} ") for marker in summary_markers)
        ),
        len(lines),
    )

    pre_summary = lines[:summary_index]
    post_summary = lines[summary_index:]
    descriptions = []
    for line in pre_summary:
        lowered = line.lower()
        if line.isdigit():
            continue
        if line.startswith("="):
            continue
        if any(
            lowered.startswith(prefix)
            for prefix in (
                "authorization:",
                "server:",
                "table",
                "guest",
                "order #",
                "check ",
                "visa ",
                "payment:",
                "method:",
                "card:",
                "driver:",
                "from:",
                "to:",
                "distance:",
                "trip time:",
                "confirmation:",
                "passenger:",
                "attendee:",
                "company:",
                "email:",
            )
        ):
            continue
        if re.search(r"\b\d{5}\b", line) or re.search(r"\b[A-Z]{2}\b", line) and "," in line:
            continue
        if line.isupper() and len(line.split()) <= 4:
            continue
        if re.search(r"\b\d{4,}\b", line) or re.search(r"\b[A-Z]{2}\s\d{5}\b", line):
            continue
        if len(line) <= 2:
            continue
        descriptions.append(line)

    amount_values = [
        amount
        for line in post_summary
        if (amount := _money_from_line(line)) is not None
    ]
    if descriptions and amount_values:
        paired_count = min(len(descriptions), len(amount_values))
        items.extend(
            {
                "description": descriptions[idx],
                "amount": amount_values[idx],
            }
            for idx in range(paired_count)
        )

    deduped: list[dict] = []
    seen: set[tuple[str, float]] = set()
    for item in items:
        key = (item["description"], item["amount"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:15]


def _extract_notes(text: str) -> list[str]:
    notes = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("NOTE:"):
            notes.append(stripped[5:].strip())
        if stripped.lower().startswith("includes:"):
            notes.append(stripped)
    return notes


def _extract_attendees(text: str) -> list[str]:
    attendees: list[str] = []
    matches = re.findall(r"Other attendees:\s*([^\n]+)", text, flags=re.IGNORECASE)
    for match in matches:
        attendees.extend([part.strip() for part in match.split(",") if part.strip()])
    if "external client" in text.lower():
        attendees.append("external_client")
    return attendees


def _extract_flight_duration(text: str) -> float | None:
    match = re.search(r"Duration\s+(\d+)h\s+(\d+)m", text)
    if not match:
        return None
    return round(int(match.group(1)) + int(match.group(2)) / 60, 2)


def _extract_class_of_service(text: str) -> str | None:
    for pattern in [
        r"Main Cabin \(Class [A-Z]\)",
        r"Premium Select \(Class [A-Z]\)",
        r"Premium Economy",
        r"Business class",
        r"First class",
        r"Wanna Get Away",
    ]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def infer_category(text: str, filename: str) -> str:
    lowered = f"{filename} {text}".lower()
    category_keywords = [
        ("meal", ["server:", "table", "breakfast", "lunch", "dinner", "cafe", "restaurant", "bbq", "tacos"]),
        ("conference", ["conference", "registration", "workshop", "attendee"]),
        ("lodging", ["hotel", "marriott", "hyatt", "hilton", "check-in", "nights:"]),
        ("air_travel", ["airlines", "flight", "e-ticket", "record locator", "confirmation:"]),
        ("ground_transport", ["uber", "lyft", "rideshare", "trip receipt"]),
    ]
    for category, keywords in category_keywords:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


def normalize_receipt_text(text: str, filename: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    category = infer_category(text, filename)
    vendor = _clean_vendor(lines)
    payment_method = _extract_payment_method(text)
    line_items = _extract_line_items(text)
    notes = _extract_notes(text)
    totals = {
        "subtotal": _extract_amount("Subtotal", text),
        "tax": _extract_amount("Tax", text),
        "tip": _extract_amount("Tip", text),
        "total": _last_amount(text)
        or _extract_amount("GRAND TOTAL", text)
        or _extract_amount("Total Charged", text)
        or _extract_amount("TOTAL", text)
        or _extract_amount("Total", text)
        ,
    }
    contains_alcohol = any(
        re.search(r"\b(beer|wine|ale|hefeweizen|cocktail|bourbon|whiskey|vodka)\b", item["description"], flags=re.IGNORECASE)
        for item in line_items
    )
    return {
        "vendor": vendor,
        "date": _extract_date(text),
        "meal_type": _extract_time_of_day(text) if category == "meal" else None,
        "category": category,
        "subtotal": totals["subtotal"],
        "tax": totals["tax"],
        "tip": totals["tip"],
        "total": totals["total"],
        "line_items": line_items,
        "payment_method": payment_method,
        "notes": notes,
        "attendees": _extract_attendees(text),
        "contains_alcohol": contains_alcohol,
        "flight_duration_hours": _extract_flight_duration(text) if category == "air_travel" else None,
        "class_of_service": _extract_class_of_service(text) if category == "air_travel" else None,
    }


def compute_extraction_confidence(text: str, normalized: dict, status: str) -> float:
    if status == "unsupported":
        return 0.0
    score = 0.2 if text.strip() else 0.0
    if normalized.get("vendor"):
        score += 0.2
    if normalized.get("date"):
        score += 0.15
    if normalized.get("total") is not None:
        score += 0.2
    if normalized.get("line_items"):
        score += 0.15
    if normalized.get("category"):
        score += 0.1
    if len(text.split()) > 25:
        score += 0.1
    if status == "needs_human_review":
        score = min(score, 0.45)
    return round(min(score, 0.99), 2)


def looks_like_receipt(normalized: dict) -> bool:
    if normalized.get("total") is not None:
        return True
    if normalized.get("date") and normalized.get("vendor"):
        return True
    if len(normalized.get("line_items", [])) >= 2:
        return True
    if normalized.get("payment_method") and normalized.get("vendor"):
        return True
    return False


def parse_trip_dates(value: str | None) -> tuple[str | None, str | None]:
    if not value or " to " not in value:
        return (None, None)
    start, end = value.split(" to ", 1)
    return start.strip(), end.strip()


def process_uploaded_receipt(upload: UploadFile, employee_id: str, submission_id: int) -> tuple[StorageResult, str]:
    filename = upload.filename or "receipt"
    file_type = detect_file_type(filename, upload.content_type)
    temp_path = settings.uploads_dir / f"{submission_id}_{filename}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(upload.file.read())
    key = f"cases/manual/{employee_id}/{submission_id}/receipts/{Path(filename).name}"
    stored = store_file(key, temp_path)
    return stored, file_type
