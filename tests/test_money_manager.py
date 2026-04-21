from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from money_manager import classifier, db, importer, web


SAMPLE_ROWS = [
    ("21.04.2026", "21.04.2026", "VISA REWE MARKT GMBH-ZW", "Lastschrift", "NR XXXX 1111 BERLIN DE KAUFUMSATZ ARN12345678900000000000001", "975,00", "EUR", "-25,00", "EUR"),
    ("20.04.2026", "20.04.2026", "Telekom Deutschland GmbH", "Lastschrift", "Festnetz Vertragskonto", "1022,95", "EUR", "-47,95", "EUR"),
    ("19.04.2026", "19.04.2026", "Sample Church", "Überweisung", "Monthly giving", "1222,95", "EUR", "-200,00", "EUR"),
    ("18.04.2026", "18.04.2026", "VISA BOLT.EU O", "Lastschrift", "NR XXXX 2222 TALLINN EE KAUFUMSATZ ARN12345678900000000000002", "1237,45", "EUR", "-14,50", "EUR"),
    ("17.04.2026", "17.04.2026", "VATTENFALL EUROPE SALES", "Lastschrift", "Strom Abschlag", "1394,45", "EUR", "-157,00", "EUR"),
    ("16.04.2026", "16.04.2026", "Sample Landlord", "Überweisung", "April rent", "2664,45", "EUR", "-1270,00", "EUR"),
    ("15.04.2026", "15.04.2026", "VISA REWE MARKT GMBH-ZW", "Lastschrift", "NR XXXX 2222 BERLIN DE KAUFUMSATZ ARN12345678900000000000003", "2694,45", "EUR", "-30,00", "EUR"),
    ("10.03.2026", "10.03.2026", "VISA RYANAIR", "Lastschrift", "Flight booking", "2794,45", "EUR", "-100,00", "EUR"),
    ("05.02.2026", "05.02.2026", "VISA BLUME 2000", "Lastschrift", "Flower order", "2814,45", "EUR", "-20,00", "EUR"),
    ("15.01.2026", "15.01.2026", "Bargeldauszahlung VISA Card", "Lastschrift", "NR XXXX 1111 BARGELDAUSZAHLUNG", "2864,45", "EUR", "-50,00", "EUR"),
    ("01.01.2026", "01.01.2026", "Sample Income", "Gutschrift", "January transfer", "4364,45", "EUR", "1500,00", "EUR"),
]


def sample_ing_csv() -> str:
    metadata = [
        "Bank;ING",
        "Kontoname;Sample Checking",
        "IBAN;DE00 0000 0000 0000 0000 0000",
        "Kunde;Sample User",
        "Zeitraum;01.01.2026 - 20.04.2026",
        "Saldo;975,00",
        "",
        "Buchung;Wertstellungsdatum;Auftraggeber/Empfänger;Buchungstext;Verwendungszweck;Saldo;Währung;Betrag;Währung",
    ]
    rows = [";".join(row) for row in SAMPLE_ROWS]
    return "\n".join(metadata + rows)


class MoneyManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "money_manager.sqlite"
        self.sample_csv = Path(self.tmp.name) / "sample_ing.csv"
        self.sample_csv.write_text(sample_ing_csv(), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_imports_ing_csv_and_deduplicates_reimport(self) -> None:
        result = importer.import_ing_csv(self.sample_csv, self.db_path)
        self.assertEqual(result.rows_seen, len(SAMPLE_ROWS))
        self.assertEqual(result.inserted, len(SAMPLE_ROWS))
        self.assertEqual(result.skipped_duplicates, 0)
        self.assertEqual(result.period_start, "2026-01-01")
        self.assertEqual(result.period_end, "2026-04-20")

        second = importer.import_ing_csv(self.sample_csv, self.db_path)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.skipped_duplicates, len(SAMPLE_ROWS))

        with db.connect(self.db_path) as conn:
            tx_count = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
            suffixes = {
                row["suffix"]
                for row in conn.execute("SELECT suffix FROM payment_instruments ORDER BY suffix")
            }
            telekom = conn.execute(
                "SELECT raw_party FROM transactions WHERE raw_party LIKE 'Telekom%' LIMIT 1"
            ).fetchone()
        self.assertEqual(tx_count, len(SAMPLE_ROWS))
        self.assertIn("1111", suffixes)
        self.assertIn("2222", suffixes)
        self.assertEqual(telekom["raw_party"], "Telekom Deutschland GmbH")

    def test_classifies_known_recurring_merchants(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        summary = classifier.classify_all(self.db_path, use_llm=False)
        self.assertEqual(summary.total_transactions, len(SAMPLE_ROWS))

        with db.connect(self.db_path) as conn:
            samples = conn.execute(
                """
                SELECT t.normalized_merchant, c.name AS category
                FROM transactions t
                JOIN transaction_classifications tc ON tc.transaction_id = t.id
                JOIN categories c ON c.id = tc.category_id
                WHERE t.normalized_merchant IN (
                    'REWE MARKT GMBH-ZW',
                    'VATTENFALL EUROPE SALES',
                    'SAMPLE LANDLORD',
                    'TELEKOM DEUTSCHLAND GMBH',
                    'SAMPLE CHURCH'
                )
                GROUP BY t.normalized_merchant, c.name
                """
            ).fetchall()
        mapping = {row["normalized_merchant"]: row["category"] for row in samples}
        self.assertEqual(mapping["REWE MARKT GMBH-ZW"], "Groceries")
        self.assertEqual(mapping["VATTENFALL EUROPE SALES"], "Utilities")
        self.assertEqual(mapping["SAMPLE LANDLORD"], "Housing/Rent")
        self.assertEqual(mapping["TELEKOM DEUTSCHLAND GMBH"], "Subscriptions/Telecom")
        self.assertEqual(mapping["SAMPLE CHURCH"], "Gifts/Charity/Church")

    def test_travel_merchants_and_cash_are_built_in_travel(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            for merchant in ("RYANAIR", "LUFTHANSA", "DB FERNVERKEHR", "BOOKING.COM", "BARGELDAUSZAHLUNG VISA"):
                with self.subTest(merchant=merchant):
                    result = classifier.classify_with_built_ins(
                        conn,
                        {
                            "normalized_merchant": merchant,
                            "raw_party": merchant,
                            "booking_text": "Lastschrift",
                            "purpose": "Manual sample",
                            "amount_cents": -1000,
                        },
                    )
                    self.assertIsNotNone(result)
                    self.assertEqual(result["category_name"], "Travel")

    def test_blume_is_built_in_shopping(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            result = classifier.classify_with_built_ins(
                conn,
                {
                    "normalized_merchant": "BLUME",
                    "raw_party": "BLUME",
                    "booking_text": "Lastschrift",
                    "purpose": "Manual sample",
                    "amount_cents": -1000,
                },
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["category_name"], "Shopping")

    def test_cash_withdrawal_category_is_retired(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            row = conn.execute("SELECT active FROM categories WHERE name = 'Cash Withdrawal'").fetchone()
        self.assertIsNone(row)

    def test_manual_review_creates_reusable_rule(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        classifier.classify_all(self.db_path, use_llm=False)
        with db.connect(self.db_path) as conn:
            tx = conn.execute(
                """
                SELECT id FROM transactions
                WHERE normalized_merchant = 'REWE MARKT GMBH-ZW'
                LIMIT 1
                """
            ).fetchone()
            travel_id = db.category_id(conn, "Travel")
            rule_id = classifier.save_manual_classification(conn, tx["id"], travel_id, create_rule=True)
            conn.commit()
            self.assertIsNotNone(rule_id)

        summary = classifier.classify_all(self.db_path, use_llm=False)
        self.assertGreaterEqual(summary.rule, 1)
        with db.connect(self.db_path) as conn:
            category = conn.execute(
                """
                SELECT c.name
                FROM transactions t
                JOIN transaction_classifications tc ON tc.transaction_id = t.id
                JOIN categories c ON c.id = tc.category_id
                WHERE t.normalized_merchant = 'REWE MARKT GMBH-ZW'
                  AND tc.reviewer_status != 'manual'
                LIMIT 1
                """
            ).fetchone()["name"]
        self.assertEqual(category, "Travel")

    def test_category_spending_trend_uses_rolling_month_window(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        classifier.classify_all(self.db_path, use_llm=False)
        with db.connect(self.db_path) as conn:
            groceries_id = db.category_id(conn, "Groceries")
            rows = web.category_spending_trend(conn, groceries_id, 6)

        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["month"], "2025-11")
        self.assertEqual(rows[-1]["month"], "2026-04")
        self.assertGreater(sum(row["amount"] for row in rows), 0)

    def test_category_top_merchants_are_scoped_to_category_window(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        classifier.classify_all(self.db_path, use_llm=False)
        with db.connect(self.db_path) as conn:
            groceries_id = db.category_id(conn, "Groceries")
            rows = web.top_merchants_for_category(conn, groceries_id, 6, limit=10)

        self.assertLessEqual(len(rows), 10)
        self.assertGreater(sum(row["amount"] for row in rows), 0)

    def test_spending_category_defaults_and_period_options(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        classifier.classify_all(self.db_path, use_llm=False)
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            categories = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order").fetchall()
            restaurants_id = db.category_id(conn, "Restaurants/Cafes")
            months = web.available_months(conn)

            self.assertEqual(web.selected_trend_category(conn, {}, categories), restaurants_id)
            self.assertEqual(web.selected_category_period({}, months), "last_3")
            self.assertIn(("last_12", "Last 1 year"), web.period_options(months))
            self.assertIn((f"month:{months[0]}", web.format_month_label(months[0])), web.period_options(months))

    def test_manual_transaction_entry_is_saved_and_classified(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            travel_id = db.category_id(conn, "Travel")
            transaction_id = web.save_manual_transaction(
                conn,
                {
                    "name": ["Weekend hotel"],
                    "amount": ["120.50"],
                    "date": ["2026-04-21"],
                    "direction": ["expense"],
                    "category_id": [str(travel_id)],
                    "note": ["manual booking"],
                },
            )
            row = conn.execute(
                """
                SELECT t.normalized_merchant, t.amount_cents, t.booking_date,
                       c.name AS category, tc.source_method, tc.reviewer_status
                FROM transactions t
                JOIN transaction_classifications tc ON tc.transaction_id = t.id
                JOIN categories c ON c.id = tc.category_id
                WHERE t.id = ?
                """,
                (transaction_id,),
            ).fetchone()

        self.assertEqual(row["normalized_merchant"], "WEEKEND HOTEL")
        self.assertEqual(row["amount_cents"], -12050)
        self.assertEqual(row["booking_date"], "2026-04-21")
        self.assertEqual(row["category"], "Travel")
        self.assertEqual(row["source_method"], "manual_entry")
        self.assertEqual(row["reviewer_status"], "manual")

    def test_transaction_category_update_marks_manual(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            groceries_id = db.category_id(conn, "Groceries")
            travel_id = db.category_id(conn, "Travel")
            transaction_id = web.save_manual_transaction(
                conn,
                {
                    "name": ["Category test"],
                    "amount": ["12.00"],
                    "date": ["2026-04-21"],
                    "direction": ["expense"],
                    "category_id": [str(groceries_id)],
                },
            )
            web.update_transaction_category(
                conn,
                {"transaction_id": [str(transaction_id)], "category_id": [str(travel_id)]},
            )
            row = conn.execute(
                """
                SELECT c.name AS category, tc.source_method, tc.reviewer_status
                FROM transaction_classifications tc
                JOIN categories c ON c.id = tc.category_id
                WHERE tc.transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()

        self.assertEqual(row["category"], "Travel")
        self.assertEqual(row["source_method"], "manual_update")
        self.assertEqual(row["reviewer_status"], "manual")

    def test_rule_update_uses_friendly_name(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            shopping_id = db.category_id(conn, "Shopping")
            conn.execute(
                """
                INSERT INTO classification_rules(
                    name, normalized_merchant_pattern, direction, category_id, priority
                )
                VALUES('OLD -> category 7', 'OLD', 'expense', ?, 20)
                """,
                (shopping_id,),
            )
            rule_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            web.update_classification_rule(
                conn,
                {
                    "rule_id": [str(rule_id)],
                    "pattern": ["Blumen Lawrenz"],
                    "direction": ["expense"],
                    "category_id": [str(shopping_id)],
                    "priority": ["15"],
                },
            )
            row = conn.execute("SELECT * FROM classification_rules WHERE id = ?", (rule_id,)).fetchone()

        self.assertEqual(row["name"], "BLUMEN LAWRENZ -> Shopping")
        self.assertEqual(row["normalized_merchant_pattern"], "BLUMEN LAWRENZ")
        self.assertEqual(row["priority"], 15)

    def test_bulk_categorize_preserves_fixed_categories(self) -> None:
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            groceries_id = db.category_id(conn, "Groceries")
            housing_id = db.category_id(conn, "Housing/Rent")
            travel_id = db.category_id(conn, "Travel")
            dinner_id = web.save_manual_transaction(
                conn,
                {
                    "name": ["Trip dinner"],
                    "amount": ["45.00"],
                    "date": ["2026-04-21"],
                    "direction": ["expense"],
                    "category_id": [str(groceries_id)],
                },
            )
            rent_id = web.save_manual_transaction(
                conn,
                {
                    "name": ["Trip rent"],
                    "amount": ["1200.00"],
                    "date": ["2026-04-21"],
                    "direction": ["expense"],
                    "category_id": [str(housing_id)],
                },
            )
            updated = web.bulk_categorize_transactions(
                conn,
                {
                    "start_at": ["2026-04-20T12:00"],
                    "end_at": ["2026-04-22T12:00"],
                    "category_id": [str(travel_id)],
                },
            )
            categories = {
                row["transaction_id"]: row["category"]
                for row in conn.execute(
                    """
                    SELECT tc.transaction_id, c.name AS category
                    FROM transaction_classifications tc
                    JOIN categories c ON c.id = tc.category_id
                    WHERE tc.transaction_id IN (?, ?)
                    """,
                    (dinner_id, rent_id),
                )
            }

        self.assertEqual(updated, 1)
        self.assertEqual(categories[dinner_id], "Travel")
        self.assertEqual(categories[rent_id], "Housing/Rent")

    def test_month_labels_use_short_month_and_year(self) -> None:
        self.assertEqual(web.format_month_label("2026-01"), "Jan 26")
        self.assertEqual(web.format_month_label("2025-12"), "Dec 25")
        self.assertEqual(web.format_month_label("2026-04-20"), "Apr 26")

    def test_full_date_label_uses_day_month_year(self) -> None:
        self.assertEqual(web.format_full_date("2026-04-15"), "15 April 2026")
        self.assertEqual(web.format_full_date("2026-04-05"), "5 April 2026")

    def test_transaction_table_prefers_card_owner_label(self) -> None:
        row = {
            "id": 1,
            "booking_date": "2026-04-15",
            "normalized_merchant": "TEST MERCHANT",
            "purpose": "sample",
            "category_id": 1,
            "card_owner": "Primary Card",
            "card_suffix": "1111",
            "amount_cents": -1000,
            "reviewer_status": "manual",
        }
        table = web.transaction_table([row], [{"id": 1, "name": "Travel"}])
        self.assertIn("Primary Card", table)
        self.assertNotIn(">1111<", table)

    def test_llm_mask_removes_sensitive_reference_tokens(self) -> None:
        importer.import_ing_csv(self.sample_csv, self.db_path)
        with db.connect(self.db_path) as conn:
            tx = conn.execute(
                "SELECT * FROM transactions WHERE purpose LIKE '%ARN%' AND purpose LIKE '%NR XXXX%' LIMIT 1"
            ).fetchone()
            categories = [row["name"] for row in conn.execute("SELECT name FROM categories")]
            payload = classifier.masked_transaction_payload(tx, categories)
        blob = " ".join(str(value) for value in payload.values())
        self.assertNotIn("ARN123", blob)
        self.assertNotIn("1111", blob)
        self.assertNotIn("2222", blob)
        self.assertIn("[ARN]", blob)
        self.assertIn("[CARD]", blob)


if __name__ == "__main__":
    unittest.main()
