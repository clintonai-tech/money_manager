from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import db


PROMPT_VERSION = "money-manager-v1"
DEFAULT_MODEL = "gpt-5.4-mini"
AUTO_ACCEPT_CONFIDENCE = 0.90


@dataclass(frozen=True)
class ClassificationSummary:
    total_transactions: int
    built_in: int
    rule: int
    llm: int
    pending: int
    skipped_manual: int


def classify_all(
    db_path: str = str(db.DEFAULT_DB_PATH),
    use_llm: bool | None = None,
    limit: int | None = None,
) -> ClassificationSummary:
    if use_llm is None:
        use_llm = bool(os.environ.get("OPENAI_API_KEY"))

    counts = {"built_in": 0, "rule": 0, "llm": 0, "pending": 0, "skipped_manual": 0}
    with db.connect(db_path) as conn:
        db.init_db(conn)
        transactions = conn.execute(
            """
            SELECT t.*, tc.reviewer_status
            FROM transactions t
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
            ORDER BY t.booking_date DESC, t.id DESC
            """ + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()

        for transaction in transactions:
            if transaction["reviewer_status"] == "manual":
                counts["skipped_manual"] += 1
                continue

            rule_result = classify_with_user_rules(conn, transaction)
            if rule_result:
                upsert_classification(conn, transaction["id"], **rule_result)
                counts["rule"] += 1
                continue

            built_in_result = classify_with_built_ins(conn, transaction)
            if built_in_result:
                upsert_classification(conn, transaction["id"], **built_in_result)
                counts["built_in"] += 1
                continue

            llm_result = classify_with_llm(conn, transaction) if use_llm else None
            if llm_result:
                upsert_classification(conn, transaction["id"], **llm_result)
                counts["llm"] += 1
                continue

            upsert_classification(
                conn,
                transaction["id"],
                category_name="Uncategorized",
                confidence=0.0,
                source_method="unclassified",
                reviewer_status="pending",
                rationale="Needs manual review.",
            )
            counts["pending"] += 1

        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

    return ClassificationSummary(
        total_transactions=int(total),
        built_in=counts["built_in"],
        rule=counts["rule"],
        llm=counts["llm"],
        pending=counts["pending"],
        skipped_manual=counts["skipped_manual"],
    )


def classify_with_user_rules(conn, transaction) -> dict[str, Any] | None:
    rules = conn.execute(
        """
        SELECT r.*, c.name AS category_name
        FROM classification_rules r
        JOIN categories c ON c.id = r.category_id
        WHERE r.active = 1
        ORDER BY r.priority ASC, r.id ASC
        """
    ).fetchall()
    merchant = transaction["normalized_merchant"].upper()
    purpose = transaction["purpose"].upper()
    amount_abs = abs(int(transaction["amount_cents"]))

    for rule in rules:
        if rule["direction"] and rule["direction"] != transaction["direction"]:
            continue
        if rule["min_amount_cents"] is not None and amount_abs < int(rule["min_amount_cents"]):
            continue
        if rule["max_amount_cents"] is not None and amount_abs > int(rule["max_amount_cents"]):
            continue
        if rule["normalized_merchant_pattern"].upper() not in merchant:
            continue
        if rule["purpose_pattern"] and rule["purpose_pattern"].upper() not in purpose:
            continue
        return {
            "category_name": rule["category_name"],
            "confidence": float(rule["confidence"]),
            "source_method": "rule",
            "reviewer_status": "confirmed",
            "rationale": f"Matched saved rule: {rule['name']}",
            "rule_id": int(rule["id"]),
        }
    return None


def classify_with_built_ins(conn, transaction) -> dict[str, Any] | None:
    merchant = transaction["normalized_merchant"].upper()
    raw_party = transaction["raw_party"].upper()
    booking_text = transaction["booking_text"].upper()
    purpose = transaction["purpose"].upper()
    haystack = f"{merchant} {raw_party} {booking_text} {purpose}"
    amount_cents = int(transaction["amount_cents"])

    if amount_cents > 0:
        if "AMAZON" in haystack or "AMZN" in haystack:
            return built("Refunds", 0.95, "Positive Amazon/payment transaction.")
        return built("Income", 0.90, "Positive incoming transaction.")

    checks: list[tuple[str, float, str, list[str]]] = [
        ("Housing/Rent", 0.98, "Rent or landlord payment.", ["LANDLORD", "RENT", "MIETE", "KISSINGER"]),
        ("Utilities", 0.98, "Utility payment.", ["VATTENFALL", "STROM", "GAS", "ENERGIE"]),
        ("Subscriptions/Telecom", 0.98, "Telecom or subscription payment.", ["TELEKOM", "AMZNPRIME", "PRIME", "NETFLIX", "SPOTIFY"]),
        ("Gifts/Charity/Church", 0.98, "Donation, church, or charity payment.", ["CHURCH", "KIRCHE", "SPENDE"]),
        ("Fees", 0.96, "Bank fee or public fee.", ["ENTGELT", "GEBUEHR", "GEBÜHR", "RUNDFUNK", "ARD"]),
        ("Groceries", 0.95, "Grocery or supermarket merchant.", ["REWE", "EDEKA", "LIDL", "NETTO", "EURO GIDA", "EUROGIDA", "GEBA", "NAH UND GUT", "ZORA SUPERMARKT", "ASIA MIGHT", "GO ASIA", "ZABKA"]),
        ("Health/Pharmacy", 0.95, "Drugstore or pharmacy merchant.", ["ROSSMANN", "DM DROGERIE", "APOTHEKE"]),
        ("Transport", 0.95, "Mobility or transport merchant.", ["BOLT", "DEUTSCHE POST", "BV G", "BVG", "UBER"]),
        ("Travel", 0.94, "Travel, lodging, cash, or activity merchant.", ["AIRBNB", "GETYOURGUIDE", "HOTEL", "BOOKING.COM", "BOOKING COM", "RYANAIR", "LUFTHANSA", "DB ", "DEUTSCHE BAHN", "BAHN", "BARGELDAUSZAHLUNG", "BARGELD"]),
        ("Household", 0.92, "Home improvement or household merchant.", ["IKEA", "BAUHAUS", "OBI", "TONERDUMPING"]),
        ("Shopping", 0.88, "Online or retail shopping merchant.", ["AMAZON", "SHEIN", "WOOLWORTH", "BLUME", "TAXFIX"]),
        ("Restaurants/Cafes", 0.86, "Restaurant or cafe-like merchant.", ["CAFE", "BISTRO", "RESTAURANT", "RAMEN", "GASTRONOMIE", "KITCHEN", "BAECKEREI", "BACKEREI", "FEINBAECKEREI", "ARIRANG", "EFES"]),
        ("Transfers/Internal", 0.86, "Transfer between people/accounts.", ["ÜBERWEISUNG", "UEBERWEISUNG", "ECHTZEITÜBERWEISUNG", "ECHTZEITUEBERWEISUNG"]),
    ]

    for category, confidence, rationale, keywords in checks:
        if any(keyword in haystack for keyword in keywords):
            return built(category, confidence, rationale)
    return None


def built(category_name: str, confidence: float, rationale: str) -> dict[str, Any]:
    status = "confirmed" if confidence >= AUTO_ACCEPT_CONFIDENCE else "pending"
    return {
        "category_name": category_name,
        "confidence": confidence,
        "source_method": "built_in",
        "reviewer_status": status,
        "rationale": rationale,
    }


def classify_with_llm(conn, transaction) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    categories = [row["name"] for row in conn.execute("SELECT name FROM categories WHERE active = 1 ORDER BY sort_order")]
    masked_input = masked_transaction_payload(transaction, categories)
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    request_body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "Classify household bank transactions into exactly one provided category. "
                    "Return only JSON with keys category, confidence, rationale, suggested_pattern."
                ),
            },
            {"role": "user", "content": json.dumps(masked_input, ensure_ascii=False)},
        ],
    }

    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    parsed = parse_openai_response(response_json)
    category_name = parsed.get("category")
    if category_name not in categories:
        category_name = "Uncategorized"
    confidence = safe_confidence(parsed.get("confidence"))
    category_id = db.category_id(conn, category_name)
    conn.execute(
        """
        INSERT INTO llm_classification_runs(
            transaction_id, prompt_version, model, masked_input_json, response_json,
            category_id, confidence, token_count
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction["id"],
            PROMPT_VERSION,
            model,
            json.dumps(masked_input, ensure_ascii=False, sort_keys=True),
            json.dumps(response_json, ensure_ascii=False, sort_keys=True),
            category_id,
            confidence,
            extract_token_count(response_json),
        ),
    )
    return {
        "category_name": category_name,
        "confidence": confidence,
        "source_method": "llm",
        "reviewer_status": "confirmed" if confidence >= AUTO_ACCEPT_CONFIDENCE else "pending",
        "rationale": parsed.get("rationale") or "LLM classification.",
    }


def masked_transaction_payload(transaction, categories: list[str]) -> dict[str, Any]:
    return {
        "merchant": mask_text(transaction["normalized_merchant"]),
        "booking_text": mask_text(transaction["booking_text"]),
        "purpose": mask_text(transaction["purpose"]),
        "direction": transaction["direction"],
        "amount_bucket": amount_bucket(int(transaction["amount_cents"])),
        "categories": categories,
    }


def mask_text(text: str) -> str:
    masked = text.upper()
    masked = re.sub(r"\bARN\d+\b", "[ARN]", masked)
    masked = re.sub(r"\bNR\s+XXXX\s+\d{4}\b", "NR XXXX [CARD]", masked)
    masked = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,}\b", "[IBAN]", masked)
    masked = re.sub(r"\b\d{6,}\b", "[NUMBER]", masked)
    masked = re.sub(r"\b\d{2}\.\d{2}\b", "[DATE]", masked)
    masked = re.sub(r"\s+", " ", masked).strip()
    return masked


def amount_bucket(amount_cents: int) -> str:
    amount = abs(amount_cents) / 100
    if amount < 10:
        return "under_10"
    if amount < 25:
        return "10_to_25"
    if amount < 50:
        return "25_to_50"
    if amount < 100:
        return "50_to_100"
    if amount < 250:
        return "100_to_250"
    if amount < 1000:
        return "250_to_1000"
    return "over_1000"


def parse_openai_response(response_json: dict[str, Any]) -> dict[str, Any]:
    text = response_json.get("output_text")
    if not text:
        chunks: list[str] = []
        for item in response_json.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        text = "\n".join(chunks)
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def extract_token_count(response_json: dict[str, Any]) -> int | None:
    usage = response_json.get("usage") or {}
    total = usage.get("total_tokens") or usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return int(total) if total else None


def safe_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def upsert_classification(
    conn,
    transaction_id: int,
    category_name: str,
    confidence: float,
    source_method: str,
    reviewer_status: str,
    rationale: str | None = None,
    rule_id: int | None = None,
) -> None:
    category_id = db.category_id(conn, category_name)
    existing = conn.execute(
        "SELECT reviewer_status FROM transaction_classifications WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchone()
    if existing and existing["reviewer_status"] == "manual":
        return

    conn.execute(
        """
        INSERT INTO transaction_classifications(
            transaction_id, category_id, confidence, source_method,
            reviewer_status, rationale, rule_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id) DO UPDATE SET
            category_id = excluded.category_id,
            confidence = excluded.confidence,
            source_method = excluded.source_method,
            reviewer_status = excluded.reviewer_status,
            rationale = excluded.rationale,
            rule_id = excluded.rule_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (transaction_id, category_id, confidence, source_method, reviewer_status, rationale, rule_id),
    )


def save_manual_classification(
    conn,
    transaction_id: int,
    category_id: int,
    create_rule: bool = True,
    note: str | None = None,
) -> int | None:
    previous = conn.execute(
        "SELECT category_id FROM transaction_classifications WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchone()
    transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if transaction is None:
        raise ValueError(f"Unknown transaction id: {transaction_id}")

    rule_id = None
    if create_rule:
        rule_id = create_rule_from_transaction(conn, transaction, category_id)

    conn.execute(
        """
        INSERT INTO transaction_classifications(
            transaction_id, category_id, confidence, source_method,
            reviewer_status, rationale, rule_id
        )
        VALUES(?, ?, 1.0, 'manual', 'manual', 'Manual review', ?)
        ON CONFLICT(transaction_id) DO UPDATE SET
            category_id = excluded.category_id,
            confidence = 1.0,
            source_method = 'manual',
            reviewer_status = 'manual',
            rationale = 'Manual review',
            rule_id = excluded.rule_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (transaction_id, category_id, rule_id),
    )
    conn.execute(
        """
        INSERT INTO manual_review_events(
            transaction_id, previous_category_id, new_category_id, rule_id, note
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            previous["category_id"] if previous else None,
            category_id,
            rule_id,
            note,
        ),
    )
    return rule_id


def create_rule_from_transaction(conn, transaction, category_id: int) -> int:
    pattern = transaction["normalized_merchant"].upper().strip()
    category = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    category_name = category["name"] if category else f"category {category_id}"
    name = f"{pattern} -> {category_name}"
    existing = conn.execute(
        """
        SELECT id FROM classification_rules
        WHERE active = 1
          AND normalized_merchant_pattern = ?
          AND direction = ?
          AND category_id = ?
        """,
        (pattern, transaction["direction"], category_id),
    ).fetchone()
    if existing:
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO classification_rules(
            name, normalized_merchant_pattern, direction, category_id,
            priority, confidence, created_from_transaction_id
        )
        VALUES(?, ?, ?, ?, 20, 0.99, ?)
        """,
        (name, pattern, transaction["direction"], category_id, transaction["id"]),
    )
    return int(cursor.lastrowid)
