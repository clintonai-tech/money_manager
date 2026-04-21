from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, classifier, db, exporter, importer, web


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="money-manager", description="Local-first ING spending dashboard")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--version", action="version", version=f"money-manager {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or migrate the SQLite database")

    import_parser = subparsers.add_parser("import", help="Import an ING CSV statement")
    import_parser.add_argument("csv_path", help="Path to ING CSV export")
    import_parser.add_argument("--classify", action="store_true", help="Run classification after import")

    classify_parser = subparsers.add_parser("classify", help="Classify imported transactions")
    classify_parser.add_argument("--no-llm", action="store_true", help="Disable OpenAI calls even if OPENAI_API_KEY is set")
    classify_parser.add_argument("--limit", type=int, help="Classify at most N transactions")

    web_parser = subparsers.add_parser("web", help="Start the local dashboard")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)

    export_parser = subparsers.add_parser("export", help="Export classified transactions to CSV")
    export_parser.add_argument("--output", help="Output CSV path")

    subparsers.add_parser("stats", help="Print compact database totals")

    args = parser.parse_args(argv)

    if args.command == "init":
        with db.connect(args.db) as conn:
            db.init_db(conn)
        print(f"Initialized database: {args.db}")
        return 0

    if args.command == "import":
        result = importer.import_ing_csv(args.csv_path, args.db)
        print(f"Imported {result.inserted} new transactions from {result.rows_seen} rows.")
        print(f"Skipped duplicates: {result.skipped_duplicates}")
        if result.period_start and result.period_end:
            print(f"Statement period: {result.period_start} to {result.period_end}")
        if args.classify:
            summary = classifier.classify_all(args.db)
            print_classification_summary(summary)
        return 0

    if args.command == "classify":
        summary = classifier.classify_all(args.db, use_llm=not args.no_llm, limit=args.limit)
        print_classification_summary(summary)
        return 0

    if args.command == "web":
        web.run_server(args.host, args.port, args.db)
        return 0

    if args.command == "export":
        output = exporter.export_transactions(args.db, args.output)
        print(f"Exported transactions: {output}")
        return 0

    if args.command == "stats":
        print_stats(Path(args.db))
        return 0

    parser.print_help()
    return 1


def print_classification_summary(summary: classifier.ClassificationSummary) -> None:
    print(f"Transactions: {summary.total_transactions}")
    print(f"Built-in classifications: {summary.built_in}")
    print(f"Saved-rule classifications: {summary.rule}")
    print(f"LLM classifications: {summary.llm}")
    print(f"Pending review: {summary.pending}")
    print(f"Manual classifications preserved: {summary.skipped_manual}")


def print_stats(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        db.init_db(conn)
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS transactions,
                COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) AS spending,
                COALESCE(SUM(amount_cents), 0) AS net
            FROM transactions
            """
        ).fetchone()
        review = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM transactions t
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
            LEFT JOIN categories c ON c.id = tc.category_id
            WHERE tc.id IS NULL OR tc.reviewer_status = 'pending' OR c.name = 'Uncategorized'
            """
        ).fetchone()
    print(f"Transactions: {row['transactions']}")
    print(f"Income: €{db.cents_to_money(row['income'])}")
    print(f"Spending: €{db.cents_to_money(row['spending'])}")
    print(f"Net: €{db.cents_to_money(row['net'])}")
    print(f"Needs review: {review['count']}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
