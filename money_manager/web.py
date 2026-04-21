from __future__ import annotations

import html
import json
import sqlite3
import urllib.parse
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import classifier, db


def run_server(host: str = "127.0.0.1", port: int = 8765, db_path: str | Path = db.DEFAULT_DB_PATH) -> None:
    database_path = str(db_path)

    class MoneyManagerHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} - {fmt % args}")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                with db.connect(database_path) as conn:
                    db.init_db(conn)
                    if parsed.path == "/":
                        body = render_overview(conn)
                    elif parsed.path == "/spending":
                        body = render_spending(conn, query)
                    elif parsed.path == "/joint":
                        body = render_joint(conn)
                    elif parsed.path == "/transactions":
                        body = render_transactions(conn, query)
                    elif parsed.path == "/review":
                        body = render_review(conn)
                    elif parsed.path == "/rules":
                        body = render_rules(conn)
                    elif parsed.path == "/categories":
                        body = render_categories(conn)
                    else:
                        self.send_error(404, "Page not found")
                        return
                self.send_html(body)
            except Exception as exc:  # noqa: BLE001
                self.send_html(page("Error", f"<main><h1>Error</h1><pre>{e(exc)}</pre></main>"), status=500)

        def do_HEAD(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/", "/spending", "/joint", "/transactions", "/review", "/rules", "/categories"}:
                self.send_error(404, "Page not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            try:
                with db.connect(database_path) as conn:
                    db.init_db(conn)
                    if parsed.path == "/review/classify":
                        transaction_id = int(first(data, "transaction_id"))
                        category_id = int(first(data, "category_id"))
                        create_rule = first(data, "create_rule", "off") == "on"
                        note = first(data, "note", "")
                        classifier.save_manual_classification(conn, transaction_id, category_id, create_rule, note)
                        conn.commit()
                        if create_rule:
                            classifier.classify_all(database_path, use_llm=False)
                        self.redirect("/review")
                        return
                    if parsed.path == "/joint/instrument":
                        instrument_id = int(first(data, "instrument_id"))
                        owner_label = first(data, "owner_label", "").strip() or None
                        conn.execute(
                            "UPDATE payment_instruments SET owner_label = ? WHERE id = ?",
                            (owner_label, instrument_id),
                        )
                        conn.commit()
                        self.redirect("/joint")
                        return
                    if parsed.path == "/transactions/add":
                        save_manual_transaction(conn, data)
                        conn.commit()
                        self.redirect("/transactions")
                        return
                    if parsed.path == "/transactions/bulk-categorize":
                        bulk_categorize_transactions(conn, data)
                        conn.commit()
                        self.redirect("/transactions")
                        return
                    if parsed.path == "/transactions/category":
                        update_transaction_category(conn, data)
                        conn.commit()
                        self.redirect(self.headers.get("Referer", "/transactions"))
                        return
                    if parsed.path == "/rules/toggle":
                        rule_id = int(first(data, "rule_id"))
                        active = int(first(data, "active"))
                        conn.execute(
                            """
                            UPDATE classification_rules
                            SET active = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (active, rule_id),
                        )
                        conn.commit()
                        self.redirect("/rules")
                        return
                    if parsed.path == "/rules/update":
                        update_classification_rule(conn, data)
                        conn.commit()
                        self.redirect("/rules")
                        return
                    if parsed.path == "/categories/add":
                        name = first(data, "name", "").strip()
                        if name:
                            next_order = conn.execute(
                                "SELECT COALESCE(MAX(sort_order), 0) + 10 AS next_order FROM categories"
                            ).fetchone()["next_order"]
                            conn.execute(
                                """
                                INSERT INTO categories(name, sort_order, active)
                                VALUES(?, ?, 1)
                                ON CONFLICT(name) DO UPDATE SET active = 1
                                """,
                                (name, next_order),
                            )
                            conn.commit()
                        self.redirect("/categories")
                        return
                    if parsed.path == "/categories/rename":
                        category_id = int(first(data, "category_id"))
                        name = first(data, "name", "").strip()
                        active = 1 if first(data, "active", "off") == "on" else 0
                        if name:
                            conn.execute(
                                """
                                UPDATE categories
                                SET name = ?, active = ?
                                WHERE id = ?
                                """,
                                (name, active, category_id),
                            )
                            conn.commit()
                        self.redirect("/categories")
                        return
                self.send_error(404, "Action not found")
            except Exception as exc:  # noqa: BLE001
                self.send_html(page("Error", f"<main><h1>Error</h1><pre>{e(exc)}</pre></main>"), status=500)

        def send_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

    server = ThreadingHTTPServer((host, port), MoneyManagerHandler)
    print(f"Money Manager dashboard: http://{host}:{port}")
    server.serve_forever()


def render_overview(conn: sqlite3.Connection) -> str:
    stats = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) AS spending,
            COALESCE(SUM(amount_cents), 0) AS net,
            COUNT(*) AS transaction_count
        FROM transactions
        """
    ).fetchone()
    balance = conn.execute(
        """
        SELECT balance_cents, booking_date
        FROM transactions
        ORDER BY booking_date DESC, value_date DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    review_count = pending_review_count(conn)
    months = conn.execute(
        """
        SELECT
            substr(booking_date, 1, 7) AS month,
            SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS income,
            SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END) AS spending,
            SUM(amount_cents) AS net
        FROM transactions
        GROUP BY month
        ORDER BY month
        """
    ).fetchall()
    category_rows = expense_by_category(conn, limit=8)

    kpis = "".join(
        kpi(label, value)
        for label, value in [
            ("Income", eur(stats["income"])),
            ("Spending", eur(stats["spending"])),
            ("Net", eur(stats["net"])),
            ("Latest balance", eur(balance["balance_cents"]) if balance else "0.00"),
            ("Last transaction", format_full_date(balance["booking_date"]) if balance else ""),
            ("Transactions", str(stats["transaction_count"])),
            ("Needs review", str(review_count)),
        ]
    )
    content = f"""
    <main>
      <header class="hero">
        <div>
          <p class="eyebrow">Local household finance</p>
          <h1>Money Manager</h1>
          <p>CSV-first ING imports, persistent SQLite history, reusable rules, and a manual review loop.</p>
        </div>
        <a class="primary" href="/review">Review {review_count}</a>
      </header>
      <section class="kpi-grid">{kpis}</section>
      <section class="two-col">
        <div>
          <h2>Monthly Flow</h2>
          {monthly_chart(months)}
        </div>
        <div>
          <h2>Largest Categories</h2>
          {bar_list(category_rows, "category", "amount")}
        </div>
      </section>
    </main>
    """
    return page("Overview", content)


def render_spending(conn: sqlite3.Connection, query: dict[str, list[str]]) -> str:
    selected_month = first(query, "month", "")
    months = available_months(conn)
    selected_period = selected_category_period(query, months)
    rows = expense_by_category(conn, month=selected_month or None, limit=5)
    merchants = top_merchants(conn, month=selected_month or None, limit=5)
    categories = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order").fetchall()
    category_options = [(str(c["id"]), c["name"]) for c in categories]
    selected_category = selected_trend_category(conn, query, categories)
    trend_rows = category_spending_trend(conn, selected_category, selected_period)
    category_merchants = top_merchants_for_category(conn, selected_category, selected_period, limit=10)
    category_name = next((c["name"] for c in categories if int(c["id"]) == selected_category), "Category")
    content = f"""
    <main>
      <div class="page-title">
        <div><h1>Spending</h1><p>Category trends and merchant concentration.</p></div>
      </div>
      <section>
        <div class="section-heading">
          <div>
            <h2>Spending Summary</h2>
            <p>Categories and top merchants for the selected month.</p>
          </div>
          {month_filter(months, selected_month, "/spending")}
        </div>
        <div class="two-col">
          <div>
            <h3>Top 5 Categories</h3>
            {bar_list(rows, "category", "amount")}
          </div>
          <div>
            <h3>Top 5 Merchants</h3>
            {bar_list(merchants, "merchant", "amount")}
          </div>
        </div>
      </section>
      <section>
        <div class="section-heading">
          <div>
            <h2>Spending by Category</h2>
            <p>{e(category_name)} over the selected period.</p>
          </div>
          <form class="compact-filter" method="get" action="/spending">
            <input type="hidden" name="month" value="{e(selected_month)}">
            {select("trend_category", category_options, str(selected_category))}
            {select("period", period_options(months), selected_period)}
            <button>Apply</button>
          </form>
        </div>
        <div class="two-col">
          <div>
            <h3>Monthly Spending</h3>
            {category_bar_chart(trend_rows, category_name)}
          </div>
          <div>
            <h3>Top 10 Merchants</h3>
            {bar_list(category_merchants, "merchant", "amount")}
          </div>
        </div>
      </section>
    </main>
    """
    return page("Spending", content)


def render_joint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT
            pi.id,
            pi.suffix,
            pi.owner_label,
            pi.first_seen,
            pi.last_seen,
            COUNT(t.id) AS tx_count,
            COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END), 0) AS spending
        FROM payment_instruments pi
        LEFT JOIN transactions t ON t.payment_instrument_id = pi.id
        GROUP BY pi.id
        ORDER BY spending DESC, pi.suffix
        """
    ).fetchall()
    body = "".join(
        f"""
        <tr>
          <td><strong>{e(row['suffix'])}</strong></td>
          <td>{e(row['owner_label'] or 'Unassigned')}</td>
          <td>{row['tx_count']}</td>
          <td class="money">{eur(row['spending'])}</td>
          <td>{e(format_month_label(row['first_seen']))} to {e(format_month_label(row['last_seen']))}</td>
          <td>
            <form method="post" action="/joint/instrument" class="inline-form">
              <input type="hidden" name="instrument_id" value="{row['id']}">
              <input name="owner_label" value="{e(row['owner_label'] or '')}" placeholder="Owner label">
              <button>Save</button>
            </form>
          </td>
        </tr>
        """
        for row in rows
    )
    content = f"""
    <main>
      <div class="page-title"><div><h1>Joint Account</h1><p>Visible card suffixes stay local and can be labelled here.</p></div></div>
      <section>
        <table>
          <thead><tr><th>Card suffix</th><th>Owner</th><th>Transactions</th><th>Spending</th><th>Seen</th><th>Label</th></tr></thead>
          <tbody>{body or '<tr><td colspan="6">No card suffixes detected yet.</td></tr>'}</tbody>
        </table>
      </section>
    </main>
    """
    return page("Joint Account", content)


def render_transactions(conn: sqlite3.Connection, query: dict[str, list[str]]) -> str:
    filters, params = build_transaction_filters(query)
    months = available_months(conn)
    categories = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order").fetchall()
    instruments = conn.execute("SELECT id, suffix, owner_label FROM payment_instruments ORDER BY suffix").fetchall()
    sql = f"""
        SELECT t.*, c.name AS category, tc.category_id, tc.reviewer_status,
               pi.suffix AS card_suffix, pi.owner_label AS card_owner
        FROM transactions t
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
        LEFT JOIN categories c ON c.id = tc.category_id
        LEFT JOIN payment_instruments pi ON pi.id = t.payment_instrument_id
        {filters}
        ORDER BY t.booking_date DESC, t.id DESC
        LIMIT 300
    """
    rows = conn.execute(sql, params).fetchall()
    table = transaction_table(rows, categories)
    today = date.today().isoformat()
    content = f"""
    <main>
      <div class="page-title"><div><h1>Transactions</h1><p>Searchable ledger with categories and local card suffixes.</p></div></div>
      <section>
        <div class="section-heading">
          <div>
            <h2>Add Transaction</h2>
            <p>Manual entries are saved directly to the local database.</p>
          </div>
        </div>
        <form class="manual-transaction-form" method="post" action="/transactions/add">
          <input name="name" placeholder="Name" required>
          <input name="amount" inputmode="decimal" placeholder="Amount" required>
          <input type="date" name="date" value="{e(today)}" required>
          {select("direction", [("expense", "Expense"), ("income", "Income")], "expense")}
          {select("category_id", [("", "Category")] + [(str(c["id"]), c["name"]) for c in categories], "")}
          <input name="note" placeholder="Optional note">
          <button>Save</button>
        </form>
      </section>
      <section>
        <div class="section-heading">
          <div>
            <h2>Add Bulk Category</h2>
            <p>Apply one category to expenses in a travel window while preserving fixed bills and giving.</p>
          </div>
        </div>
        <form class="bulk-category-form" method="post" action="/transactions/bulk-categorize">
          <input type="datetime-local" name="start_at" required>
          <input type="datetime-local" name="end_at" required>
          {select("category_id", [("", "Category")] + [(str(c["id"]), c["name"]) for c in categories], "")}
          <button>Apply</button>
        </form>
      </section>
      <section>
        <div class="section-heading">
          <div>
            <h2>All Transactions</h2>
            <p>Search and filter the full local ledger.</p>
          </div>
        </div>
        <form class="filters" method="get" action="/transactions">
          <input name="q" value="{e(first(query, 'q', ''))}" placeholder="Search merchant or purpose">
          {select("month", [("", "All months")] + [(m, format_month_label(m)) for m in months], first(query, "month", ""))}
          {select("category", [("", "All categories")] + [(str(c["id"]), c["name"]) for c in categories], first(query, "category", ""))}
          {select("instrument", [("", "All cards")] + [(str(i["id"]), instrument_label(i)) for i in instruments], first(query, "instrument", ""))}
          <button>Filter</button>
        </form>
        {table}
      </section>
    </main>
    """
    return page("Transactions", content)


def save_manual_transaction(conn: sqlite3.Connection, data: dict[str, list[str]]) -> int:
    name = first(data, "name", "").strip()
    if not name:
        raise ValueError("Transaction name is required.")

    booking_date = date.fromisoformat(first(data, "date", "").strip()).isoformat()
    direction = first(data, "direction", "expense")
    if direction not in {"expense", "income"}:
        raise ValueError("Transaction type must be Expense or Income.")

    amount_cents = abs(parse_form_money(first(data, "amount", "")))
    signed_amount = amount_cents if direction == "income" else -amount_cents
    transaction_direction = "income" if signed_amount > 0 else "expense" if signed_amount < 0 else "zero"
    category_id = int(first(data, "category_id"))
    category = conn.execute("SELECT id FROM categories WHERE id = ? AND active = 1", (category_id,)).fetchone()
    if category is None:
        raise ValueError("Choose an active category.")
    note = first(data, "note", "").strip()
    account_id, import_id = ensure_manual_import(conn)
    merchant = normalize_manual_merchant(name)
    raw_row = {
        "source": "manual",
        "name": name,
        "amount_cents": signed_amount,
        "date": booking_date,
        "direction": direction,
        "category_id": category_id,
        "note": note,
    }
    fingerprint = f"manual:{uuid.uuid4()}"
    cursor = conn.execute(
        """
        INSERT INTO transactions(
            account_id, import_id, booking_date, value_date, raw_party,
            normalized_merchant, booking_text, purpose, amount_cents,
            currency, balance_cents, direction, payment_instrument_id,
            raw_row_json, fingerprint
        )
        VALUES(?, ?, ?, ?, ?, ?, 'Manual entry', ?, ?, 'EUR', NULL, ?, NULL, ?, ?)
        """,
        (
            account_id,
            import_id,
            booking_date,
            booking_date,
            name,
            merchant,
            note or "Manual entry",
            signed_amount,
            transaction_direction,
            json.dumps(raw_row, ensure_ascii=False, sort_keys=True),
            fingerprint,
        ),
    )
    transaction_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO transaction_classifications(
            transaction_id, category_id, confidence, source_method,
            reviewer_status, rationale
        )
        VALUES(?, ?, 1.0, 'manual_entry', 'manual', 'Manual transaction entry')
        """,
        (transaction_id, category_id),
    )
    return transaction_id


def update_transaction_category(conn: sqlite3.Connection, data: dict[str, list[str]]) -> None:
    transaction_id = int(first(data, "transaction_id"))
    category_id = int(first(data, "category_id"))
    category = conn.execute("SELECT id FROM categories WHERE id = ? AND active = 1", (category_id,)).fetchone()
    if category is None:
        raise ValueError("Choose an active category.")
    conn.execute(
        """
        INSERT INTO transaction_classifications(
            transaction_id, category_id, confidence, source_method,
            reviewer_status, rationale
        )
        VALUES(?, ?, 1.0, 'manual_update', 'manual', 'Manual category update')
        ON CONFLICT(transaction_id) DO UPDATE SET
            category_id = excluded.category_id,
            confidence = 1.0,
            source_method = 'manual_update',
            reviewer_status = 'manual',
            rationale = 'Manual category update',
            rule_id = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (transaction_id, category_id),
    )


def bulk_categorize_transactions(conn: sqlite3.Connection, data: dict[str, list[str]]) -> int:
    start_date = form_date_part(first(data, "start_at", ""))
    end_date = form_date_part(first(data, "end_at", ""))
    if start_date > end_date:
        raise ValueError("Start date must be before end date.")

    category_id = int(first(data, "category_id"))
    category = conn.execute("SELECT id FROM categories WHERE id = ? AND active = 1", (category_id,)).fetchone()
    if category is None:
        raise ValueError("Choose an active category.")

    protected_categories = (
        "Housing/Rent",
        "Utilities",
        "Subscriptions/Telecom",
        "Gifts/Charity/Church",
    )
    rows = conn.execute(
        f"""
        SELECT t.id
        FROM transactions t
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
        LEFT JOIN categories c ON c.id = tc.category_id
        WHERE substr(t.booking_date, 1, 10) BETWEEN ? AND ?
          AND t.amount_cents < 0
          AND (c.name IS NULL OR c.name NOT IN ({", ".join("?" for _ in protected_categories)}))
        """,
        (start_date, end_date, *protected_categories),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO transaction_classifications(
                transaction_id, category_id, confidence, source_method,
                reviewer_status, rationale
            )
            VALUES(?, ?, 1.0, 'manual_range', 'manual', 'Manual range category override')
            ON CONFLICT(transaction_id) DO UPDATE SET
                category_id = excluded.category_id,
                confidence = 1.0,
                source_method = 'manual_range',
                reviewer_status = 'manual',
                rationale = 'Manual range category override',
                rule_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (row["id"], category_id),
        )
    return len(rows)


def form_date_part(value: str) -> str:
    if not value:
        raise ValueError("Date range is required.")
    return date.fromisoformat(value[:10]).isoformat()


def ensure_manual_import(conn: sqlite3.Connection) -> tuple[int, int]:
    account = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
    if account is None:
        conn.execute(
            """
            INSERT INTO accounts(bank_name, account_name, iban_masked, owner_label)
            VALUES('Manual', 'Manual Entries', NULL, NULL)
            """
        )
        account_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    else:
        account_id = int(account["id"])

    source_hash = f"manual-entry-v1-{account_id}"
    conn.execute(
        """
        INSERT INTO statement_imports(
            account_id, source_filename, source_hash, row_count, raw_metadata_json
        )
        VALUES(?, 'manual-entry', ?, 0, '{"source":"manual"}')
        ON CONFLICT(source_hash) DO NOTHING
        """,
        (account_id, source_hash),
    )
    import_row = conn.execute(
        "SELECT id FROM statement_imports WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    return account_id, int(import_row["id"])


def normalize_manual_merchant(name: str) -> str:
    return " ".join(name.upper().split())


def parse_form_money(text: str) -> int:
    clean = text.strip().replace(" ", "")
    if not clean:
        raise ValueError("Amount is required.")
    if "," in clean:
        return db.money_to_cents(clean)
    return int((Decimal(clean) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def update_classification_rule(conn: sqlite3.Connection, data: dict[str, list[str]]) -> None:
    rule_id = int(first(data, "rule_id"))
    pattern = first(data, "pattern", "").upper().strip()
    if not pattern:
        raise ValueError("Merchant pattern is required.")
    direction = first(data, "direction", "") or None
    if direction not in {None, "expense", "income", "zero"}:
        raise ValueError("Unknown rule direction.")
    category_id = int(first(data, "category_id"))
    category = conn.execute("SELECT name FROM categories WHERE id = ? AND active = 1", (category_id,)).fetchone()
    if category is None:
        raise ValueError("Choose an active category.")
    priority = int(first(data, "priority", "100"))
    conn.execute(
        """
        UPDATE classification_rules
        SET name = ?,
            normalized_merchant_pattern = ?,
            direction = ?,
            category_id = ?,
            priority = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (friendly_rule_name(pattern, category["name"]), pattern, direction, category_id, priority, rule_id),
    )


def rule_display_name(row) -> str:
    name = row["name"] or ""
    generated_suffix = f" -> category {row['category_id']}"
    if name.endswith(generated_suffix):
        return friendly_rule_name(row["normalized_merchant_pattern"], row["category"])
    return name or friendly_rule_name(row["normalized_merchant_pattern"], row["category"])


def friendly_rule_name(pattern: str, category_name: str) -> str:
    return f"{pattern} -> {category_name}"


def render_review(conn: sqlite3.Connection) -> str:
    categories = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order").fetchall()
    rows = conn.execute(
        """
        SELECT t.*, c.name AS category, tc.confidence, tc.source_method, tc.rationale
        FROM transactions t
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
        LEFT JOIN categories c ON c.id = tc.category_id
        WHERE tc.id IS NULL
           OR tc.reviewer_status = 'pending'
           OR c.name = 'Uncategorized'
        ORDER BY t.booking_date DESC, t.id DESC
        LIMIT 200
        """
    ).fetchall()
    items = []
    for row in rows:
        selected = next((str(c["id"]) for c in categories if c["name"] == row["category"]), "")
        items.append(
            f"""
            <article class="review-item">
              <div>
                <strong>{e(row['normalized_merchant'])}</strong>
                <p>{e(format_month_label(row['booking_date']))} · {e(row['booking_text'])} · {e(row['purpose'])}</p>
                <p class="muted">Current: {e(row['category'] or 'Unclassified')} · {e(row['source_method'] or 'none')} · confidence {row['confidence'] if row['confidence'] is not None else 'n/a'}</p>
              </div>
              <form method="post" action="/review/classify" class="review-form">
                <input type="hidden" name="transaction_id" value="{row['id']}">
                {select("category_id", [(str(c["id"]), c["name"]) for c in categories], selected)}
                <label class="check"><input type="checkbox" name="create_rule" checked> Save as rule</label>
                <input name="note" placeholder="Optional note">
                <button>Confirm</button>
              </form>
              <div class="amount">{eur(row['amount_cents'])}</div>
            </article>
            """
        )
    content = f"""
    <main>
      <div class="page-title"><div><h1>Review Queue</h1><p>Confirm uncertain classifications and teach future imports.</p></div></div>
      <section class="review-list">{''.join(items) or '<p class="empty">Nothing needs review.</p>'}</section>
    </main>
    """
    return page("Review Queue", content)


def render_rules(conn: sqlite3.Connection) -> str:
    categories = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order").fetchall()
    rows = conn.execute(
        """
        SELECT r.*, c.name AS category
        FROM classification_rules r
        JOIN categories c ON c.id = r.category_id
        ORDER BY r.active DESC, r.priority ASC, r.id DESC
        """
    ).fetchall()
    body = "".join(
        f"""
        <tr>
          <td>{e(rule_display_name(row))}</td>
          <td><input form="rule-{row['id']}" name="pattern" value="{e(row['normalized_merchant_pattern'])}"></td>
          <td>{select_for_form("direction", [("", "Any"), ("expense", "Expense"), ("income", "Income"), ("zero", "Zero")], row['direction'] or "", f"rule-{row['id']}")}</td>
          <td>{select_for_form("category_id", [(str(c["id"]), c["name"]) for c in categories], str(row["category_id"]), f"rule-{row['id']}")}</td>
          <td><input class="priority-input" form="rule-{row['id']}" type="number" name="priority" value="{row['priority']}" min="0" step="1"></td>
          <td>{'Active' if row['active'] else 'Paused'}</td>
          <td><div class="rule-actions">
            <form id="rule-{row['id']}" method="post" action="/rules/update" class="inline-form">
              <input type="hidden" name="rule_id" value="{row['id']}">
              <button>Save</button>
            </form>
            <form method="post" action="/rules/toggle">
              <input type="hidden" name="rule_id" value="{row['id']}">
              <input type="hidden" name="active" value="{0 if row['active'] else 1}">
              <button>{'Pause' if row['active'] else 'Activate'}</button>
            </form>
          </div></td>
        </tr>
        """
        for row in rows
    )
    content = f"""
    <main>
      <div class="page-title"><div><h1>Rules</h1><p>Reusable memory from manual review decisions. Pause disables a rule without deleting it.</p></div></div>
      <section>
        <table>
          <thead><tr><th>Rule</th><th>Merchant pattern</th><th>Direction</th><th>Category</th><th>Priority</th><th>Status</th><th></th></tr></thead>
          <tbody>{body or '<tr><td colspan="7">No saved rules yet.</td></tr>'}</tbody>
        </table>
      </section>
    </main>
    """
    return page("Rules", content)


def render_categories(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    body = "".join(
        f"""
        <tr>
          <td>{row['id']}</td>
          <td>
            <form method="post" action="/categories/rename" class="inline-form">
              <input type="hidden" name="category_id" value="{row['id']}">
              <input name="name" value="{e(row['name'])}">
              <label class="check"><input type="checkbox" name="active" {'checked' if row['active'] else ''}> Active</label>
              <button>Save</button>
            </form>
          </td>
          <td>{row['sort_order']}</td>
        </tr>
        """
        for row in rows
    )
    content = f"""
    <main>
      <div class="page-title"><div><h1>Categories</h1><p>Add or rename the local spending taxonomy.</p></div></div>
      <section>
        <form method="post" action="/categories/add" class="filters">
          <input name="name" placeholder="New category name">
          <button>Add category</button>
        </form>
        <table>
          <thead><tr><th>ID</th><th>Category</th><th>Sort</th></tr></thead>
          <tbody>{body}</tbody>
        </table>
      </section>
    </main>
    """
    return page("Categories", content)


def expense_by_category(conn: sqlite3.Connection, month: str | None = None, limit: int = 20):
    where = "WHERE t.amount_cents < 0"
    params: list[Any] = []
    if month:
        where += " AND substr(t.booking_date, 1, 7) = ?"
        params.append(month)
    return conn.execute(
        f"""
        SELECT COALESCE(c.name, 'Unclassified') AS category,
               SUM(-t.amount_cents) AS amount
        FROM transactions t
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
        LEFT JOIN categories c ON c.id = tc.category_id
        {where}
        GROUP BY category
        ORDER BY amount DESC
        LIMIT {int(limit)}
        """,
        params,
    ).fetchall()


def top_merchants(conn: sqlite3.Connection, month: str | None = None, limit: int = 10):
    where = "WHERE amount_cents < 0"
    params: list[Any] = []
    if month:
        where += " AND substr(booking_date, 1, 7) = ?"
        params.append(month)
    return conn.execute(
        f"""
        SELECT normalized_merchant AS merchant, SUM(-amount_cents) AS amount
        FROM transactions
        {where}
        GROUP BY normalized_merchant
        ORDER BY amount DESC
        LIMIT {int(limit)}
        """,
        params,
    ).fetchall()


def top_merchants_for_category(conn: sqlite3.Connection, category_id: int, period: str | int, limit: int = 10):
    months = selected_period_months(conn, period)
    if not months or not category_id:
        return []

    placeholders = ", ".join("?" for _ in months)
    return conn.execute(
        f"""
        SELECT t.normalized_merchant AS merchant,
               SUM(-t.amount_cents) AS amount
        FROM transactions t
        JOIN transaction_classifications tc ON tc.transaction_id = t.id
        WHERE t.amount_cents < 0
          AND tc.category_id = ?
          AND substr(t.booking_date, 1, 7) IN ({placeholders})
        GROUP BY t.normalized_merchant
        ORDER BY amount DESC
        LIMIT {int(limit)}
        """,
        [category_id, *months],
    ).fetchall()


def selected_trend_category(conn: sqlite3.Connection, query: dict[str, list[str]], categories) -> int:
    category_ids = {int(row["id"]) for row in categories}
    requested = first(query, "trend_category", "")
    if requested:
        try:
            category_id = int(requested)
        except ValueError:
            category_id = 0
        if category_id in category_ids:
            return category_id

    default = next((row for row in categories if row["name"] == "Restaurants/Cafes"), None)
    if default:
        return int(default["id"])

    top = conn.execute(
        """
        SELECT tc.category_id
        FROM transactions t
        JOIN transaction_classifications tc ON tc.transaction_id = t.id
        JOIN categories c ON c.id = tc.category_id
        WHERE t.amount_cents < 0
          AND c.active = 1
        GROUP BY tc.category_id
        ORDER BY SUM(-t.amount_cents) DESC
        LIMIT 1
        """
    ).fetchone()
    if top:
        return int(top["category_id"])
    return int(categories[0]["id"]) if categories else 0


def selected_category_period(query: dict[str, list[str]], months: list[str]) -> str:
    requested = first(query, "period", "last_3")
    valid_periods = {"last_3", "last_6", "last_12"}
    if requested in valid_periods:
        return requested
    if requested.startswith("month:") and requested.removeprefix("month:") in months:
        return requested
    return "last_3"


def period_options(months: list[str]) -> list[tuple[str, str]]:
    return [
        ("last_3", "Last 3 months"),
        ("last_6", "Last 6 months"),
        ("last_12", "Last 1 year"),
        *[(f"month:{month}", format_month_label(month)) for month in months],
    ]


def category_spending_trend(conn: sqlite3.Connection, category_id: int, period: str | int):
    months = selected_period_months(conn, period)
    if not months:
        return []

    placeholders = ", ".join("?" for _ in months)
    rows = conn.execute(
        f"""
        SELECT substr(t.booking_date, 1, 7) AS month,
               SUM(-t.amount_cents) AS amount
        FROM transactions t
        JOIN transaction_classifications tc ON tc.transaction_id = t.id
        WHERE t.amount_cents < 0
          AND tc.category_id = ?
          AND substr(t.booking_date, 1, 7) IN ({placeholders})
        GROUP BY month
        """,
        [category_id, *months],
    ).fetchall()
    by_month = {row["month"]: int(row["amount"] or 0) for row in rows}
    return [{"month": month, "amount": by_month.get(month, 0)} for month in months]


def selected_period_months(conn: sqlite3.Connection, period: str | int) -> list[str]:
    if isinstance(period, int):
        return latest_month_window(conn, period)
    if period.startswith("month:"):
        return [period.removeprefix("month:")]
    counts = {"last_3": 3, "last_6": 6, "last_12": 12}
    return latest_month_window(conn, counts.get(period, 3))


def latest_month_window(conn: sqlite3.Connection, month_count: int) -> list[str]:
    latest = conn.execute("SELECT MAX(substr(booking_date, 1, 7)) AS month FROM transactions").fetchone()["month"]
    return month_window(latest, month_count)


def month_window(latest_month: str | None, month_count: int) -> list[str]:
    if not latest_month:
        return []
    year, month = (int(part) for part in latest_month.split("-", 1))
    start = add_months(date(year, month, 1), -(month_count - 1))
    return [add_months(start, offset).strftime("%Y-%m") for offset in range(month_count)]


def add_months(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def available_months(conn: sqlite3.Connection) -> list[str]:
    return [
        row["month"]
        for row in conn.execute(
            "SELECT DISTINCT substr(booking_date, 1, 7) AS month FROM transactions ORDER BY month DESC"
        )
    ]


def pending_review_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM transactions t
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.id
        LEFT JOIN categories c ON c.id = tc.category_id
        WHERE tc.id IS NULL OR tc.reviewer_status = 'pending' OR c.name = 'Uncategorized'
        """
    ).fetchone()
    return int(row["count"])


def build_transaction_filters(query: dict[str, list[str]]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    q = first(query, "q", "").strip()
    if q:
        clauses.append("(t.normalized_merchant LIKE ? OR t.raw_party LIKE ? OR t.purpose LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    month = first(query, "month", "")
    if month:
        clauses.append("substr(t.booking_date, 1, 7) = ?")
        params.append(month)
    category = first(query, "category", "")
    if category:
        clauses.append("tc.category_id = ?")
        params.append(int(category))
    instrument = first(query, "instrument", "")
    if instrument:
        clauses.append("t.payment_instrument_id = ?")
        params.append(int(instrument))
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def transaction_table(rows, categories) -> str:
    body = "".join(
        f"""
        <tr>
          <td>{e(format_month_label(row['booking_date']))}</td>
          <td>{e(row['normalized_merchant'])}<br><span>{e(row['purpose'][:90])}</span></td>
          <td>
            <form method="post" action="/transactions/category" class="category-update-form">
              <input type="hidden" name="transaction_id" value="{row['id']}">
              {select_autosubmit("category_id", [(str(c["id"]), c["name"]) for c in categories], str(row["category_id"] or ""))}
              <noscript><button>Save</button></noscript>
            </form>
          </td>
          <td>{e(row['card_owner'] or row['card_suffix'] or '')}</td>
          <td class="money">{eur(row['amount_cents'])}</td>
          <td>{e(row['reviewer_status'] or '')}</td>
        </tr>
        """
        for row in rows
    )
    return f"""
    <table>
      <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Card</th><th>Amount</th><th>Status</th></tr></thead>
      <tbody>{body or '<tr><td colspan="6">No transactions match the current filters.</td></tr>'}</tbody>
    </table>
    """


def kpi(label: str, value: str) -> str:
    return f"<article class=\"kpi\"><span>{e(label)}</span><strong>{e(value)}</strong></article>"


def monthly_chart(rows) -> str:
    if not rows:
        return '<p class="empty">No monthly data yet.</p>'
    max_value = max(max(row["income"] or 0, row["spending"] or 0) for row in rows) or 1
    bars = []
    for row in rows:
        income_width = int(((row["income"] or 0) / max_value) * 100)
        spending_width = int(((row["spending"] or 0) / max_value) * 100)
        bars.append(
            f"""
            <div class="month-row">
              <span>{e(format_month_label(row['month']))}</span>
              <div class="stack">
                <i class="income" style="width:{income_width}%"></i>
                <i class="spending" style="width:{spending_width}%"></i>
              </div>
              <strong>{eur(row['net'])}</strong>
            </div>
            """
        )
    return '<div class="chart-legend"><span class="income-dot"></span> Income <span class="spending-dot"></span> Spending</div><div class="month-chart">' + "".join(bars) + "</div>"


def bar_list(rows, label_key: str, value_key: str) -> str:
    if not rows:
        return '<p class="empty">No data yet.</p>'
    max_value = max(row[value_key] or 0 for row in rows) or 1
    return '<div class="bar-list">' + "".join(
        f"""
        <div class="bar-row">
          <span>{e(row[label_key])}</span>
          <div><i style="width:{int(((row[value_key] or 0) / max_value) * 100)}%"></i></div>
          <strong>{eur(row[value_key])}</strong>
        </div>
        """
        for row in rows
    ) + "</div>"


def category_bar_chart(rows, category_name: str) -> str:
    if not rows:
        return '<p class="empty">No data yet.</p>'
    max_value = max(row["amount"] or 0 for row in rows) or 1
    bars = "".join(
        f"""
        <div class="vertical-bar">
          <div class="bar-track"><i style="height:{int(((row['amount'] or 0) / max_value) * 100)}%"></i></div>
          <strong>{eur(row['amount'])}</strong>
          <span>{e(format_month_label(row['month']))}</span>
        </div>
        """
        for row in rows
    )
    return f"""
    <div class="vertical-chart">
      <div class="y-axis-label">{e(category_name)}</div>
      <div class="vertical-bars">{bars}</div>
    </div>
    """


def month_filter(months: list[str], selected: str, action: str) -> str:
    return f"""
    <form class="compact-filter" method="get" action="{action}">
      {select("month", [("", "All months")] + [(m, format_month_label(m)) for m in months], selected)}
      <button>Apply</button>
    </form>
    """


def instrument_label(row) -> str:
    return row["owner_label"] or row["suffix"]


def format_full_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return parsed.strftime("%d %B %Y").lstrip("0")


def format_month_label(value: str | None) -> str:
    if not value:
        return ""
    try:
        year, month = (int(part) for part in value[:7].split("-", 1))
    except ValueError:
        return value
    return date(year, month, 1).strftime("%b %y")


def select(name: str, options: list[tuple[str, str]], selected: str) -> str:
    return f'<select name="{e(name)}">' + "".join(
        f'<option value="{e(value)}" {"selected" if value == selected else ""}>{e(label)}</option>'
        for value, label in options
    ) + "</select>"


def select_for_form(name: str, options: list[tuple[str, str]], selected: str, form_id: str) -> str:
    return f'<select name="{e(name)}" form="{e(form_id)}">' + "".join(
        f'<option value="{e(value)}" {"selected" if value == selected else ""}>{e(label)}</option>'
        for value, label in options
    ) + "</select>"


def select_autosubmit(name: str, options: list[tuple[str, str]], selected: str) -> str:
    return f'<select name="{e(name)}" onchange="this.form.submit()">' + "".join(
        f'<option value="{e(value)}" {"selected" if value == selected else ""}>{e(label)}</option>'
        for value, label in options
    ) + "</select>"


def page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} · Money Manager</title>
  <style>{CSS}</style>
</head>
<body>
  <nav>
    <a href="/" class="brand">Money Manager</a>
    <a href="/spending">Spending</a>
    <a href="/joint">Joint</a>
    <a href="/transactions">Transactions</a>
    <a href="/review">Review</a>
    <a href="/rules">Rules</a>
    <a href="/categories">Categories</a>
  </nav>
  {content}
</body>
</html>"""


def first(data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = data.get(key)
    return values[0] if values else default


def eur(cents: int | None) -> str:
    return f"€{db.cents_to_money(int(cents or 0))}"


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


CSS = """
:root {
  --ink: #17202a;
  --muted: #5f6f7a;
  --line: #d9e1e5;
  --paper: #f8faf8;
  --panel: #ffffff;
  --green: #2f7d5b;
  --blue: #2a6f97;
  --red: #b44949;
  --gold: #b6862c;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--paper);
}
nav {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  gap: 18px;
  align-items: center;
  padding: 14px clamp(16px, 4vw, 44px);
  border-bottom: 1px solid var(--line);
  background: rgba(248,250,248,0.96);
}
nav a { color: var(--muted); text-decoration: none; font-weight: 650; }
nav .brand { color: var(--ink); margin-right: auto; }
main { max-width: 1180px; margin: 0 auto; padding: 28px clamp(16px, 4vw, 44px) 56px; }
h1, h2, h3 { margin: 0; letter-spacing: 0; }
h1 { font-size: clamp(32px, 5vw, 58px); line-height: 1.02; }
h2 { font-size: 22px; margin-bottom: 16px; }
h3 { font-size: 15px; margin-bottom: 14px; text-transform: uppercase; color: var(--muted); }
p { color: var(--muted); line-height: 1.5; }
.hero {
  display: flex;
  min-height: 220px;
  gap: 24px;
  align-items: center;
  justify-content: space-between;
  padding: 26px 0 34px;
}
.eyebrow { color: var(--green); font-weight: 800; text-transform: uppercase; font-size: 12px; }
.primary, button {
  border: 0;
  border-radius: 7px;
  background: var(--ink);
  color: white;
  padding: 10px 14px;
  font-weight: 750;
  text-decoration: none;
  cursor: pointer;
}
button { font-size: 14px; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 30px;
}
.kpi, section, .review-item {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.kpi { padding: 14px; min-height: 88px; }
.kpi span { color: var(--muted); display: block; font-size: 13px; }
.kpi strong { display: block; font-size: 24px; margin-top: 8px; overflow-wrap: anywhere; }
section { padding: 18px; }
.two-col { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 18px; background: transparent; border: 0; padding: 0; }
main > section + section { margin-top: 18px; }
.page-title { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin: 10px 0 24px; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 16px; }
.section-heading p { margin: 4px 0 0; }
.section-heading h2 { margin-bottom: 0; }
.bar-list, .month-chart { display: grid; gap: 12px; }
.bar-row, .month-row {
  display: grid;
  grid-template-columns: minmax(150px, 1.2fr) minmax(120px, 2fr) minmax(88px, auto);
  gap: 12px;
  align-items: center;
}
.bar-row span, .month-row span { color: var(--ink); overflow-wrap: anywhere; }
.bar-row div, .stack {
  height: 12px;
  background: #e8eef0;
  border-radius: 999px;
  overflow: hidden;
}
.bar-row i, .stack i { display: block; height: 100%; border-radius: 999px; }
.bar-row i { background: var(--blue); }
.stack { display: flex; height: 14px; }
.stack .income { background: var(--green); }
.stack .spending { background: var(--red); }
.vertical-chart {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 14px;
  align-items: stretch;
}
.y-axis-label {
  writing-mode: vertical-rl;
  transform: rotate(180deg);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-weight: 750;
  text-align: center;
}
.vertical-bars {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(68px, 1fr);
  gap: 12px;
  min-height: 270px;
  overflow-x: auto;
  padding: 4px 2px 2px;
}
.vertical-bar {
  display: grid;
  grid-template-rows: minmax(160px, 1fr) 22px 22px;
  gap: 8px;
  min-width: 68px;
  align-items: end;
  text-align: center;
}
.bar-track {
  height: 100%;
  width: 100%;
  display: flex;
  align-items: end;
  justify-content: center;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(to top, #edf2f0 1px, transparent 1px);
  background-size: 100% 25%;
}
.bar-track i {
  display: block;
  width: min(42px, 70%);
  min-height: 2px;
  background: var(--blue);
  border-radius: 7px 7px 0 0;
}
.vertical-bar strong {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.vertical-bar span {
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.chart-legend { color: var(--muted); margin-bottom: 12px; }
.income-dot, .spending-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-left: 10px;
}
.income-dot { background: var(--green); }
.spending-dot { background: var(--red); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 11px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
td span, .muted { color: var(--muted); font-size: 13px; }
.money, .amount { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.filters, .compact-filter, .inline-form, .review-form, .category-update-form { display: flex; gap: 10px; align-items: center; }
.compact-filter { flex-wrap: wrap; }
.filters { flex-wrap: wrap; margin-bottom: 16px; }
.category-update-form select { min-width: 170px; }
.rule-actions { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
.priority-input { width: 72px; min-width: 72px; }
.manual-transaction-form {
  display: grid;
  grid-template-columns: minmax(180px, 1.4fr) minmax(110px, 0.7fr) minmax(140px, 0.8fr) minmax(130px, 0.8fr) minmax(180px, 1fr) minmax(180px, 1.2fr) auto;
  gap: 10px;
  align-items: center;
}
.bulk-category-form {
  display: grid;
  grid-template-columns: minmax(190px, 1fr) minmax(190px, 1fr) minmax(180px, 1fr) auto;
  gap: 10px;
  align-items: center;
}
input, select {
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: white;
  padding: 8px 10px;
  color: var(--ink);
}
.filters input { min-width: min(340px, 100%); }
.review-list { display: grid; gap: 12px; background: transparent; border: 0; padding: 0; }
.review-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(260px, 430px) 100px;
  gap: 14px;
  align-items: center;
  padding: 16px;
}
.review-item p { margin: 4px 0 0; }
.review-form { flex-wrap: wrap; justify-content: flex-end; }
.check { color: var(--muted); white-space: nowrap; }
.empty { margin: 0; color: var(--muted); }
code { background: #edf2f0; padding: 2px 5px; border-radius: 4px; }
@media (max-width: 860px) {
  nav { overflow-x: auto; }
  .hero, .page-title, .section-heading { align-items: flex-start; flex-direction: column; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .two-col { grid-template-columns: 1fr; }
  .manual-transaction-form, .bulk-category-form { grid-template-columns: 1fr; }
  .bar-row, .month-row, .review-item { grid-template-columns: 1fr; }
  .vertical-chart { grid-template-columns: 1fr; }
  .y-axis-label { writing-mode: initial; transform: none; justify-content: flex-start; }
  .amount, .money { text-align: left; }
}
"""
