from __future__ import annotations

import csv
from pathlib import Path

from . import db


def export_transactions(
    db_path: str | Path = db.DEFAULT_DB_PATH,
    output_path: str | Path | None = None,
) -> Path:
    if output_path is None:
        output_path = db.PROJECT_ROOT / "exports" / "transactions.csv"
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                t.booking_date,
                t.value_date,
                t.raw_party,
                t.normalized_merchant,
                t.booking_text,
                t.purpose,
                t.amount_cents,
                t.currency,
                t.balance_cents,
                t.direction,
                pi.suffix AS card_suffix,
                c.name AS category,
                tc.confidence,
                tc.source_method,
                tc.reviewer_status
            FROM transactions t
            LEFT JOIN payment_instruments pi ON pi.id = t.payment_instrument_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
            LEFT JOIN categories c ON c.id = tc.category_id
            ORDER BY t.booking_date DESC, t.id DESC
            """
        ).fetchall()

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "booking_date",
                "value_date",
                "party",
                "merchant",
                "booking_text",
                "purpose",
                "amount",
                "currency",
                "balance",
                "direction",
                "card_suffix",
                "category",
                "confidence",
                "source_method",
                "reviewer_status",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["booking_date"],
                    row["value_date"],
                    row["raw_party"],
                    row["normalized_merchant"],
                    row["booking_text"],
                    row["purpose"],
                    db.cents_to_money(row["amount_cents"]),
                    row["currency"],
                    db.cents_to_money(row["balance_cents"]),
                    row["direction"],
                    row["card_suffix"] or "",
                    row["category"] or "",
                    row["confidence"] if row["confidence"] is not None else "",
                    row["source_method"] or "",
                    row["reviewer_status"] or "",
                ]
            )
    return output
