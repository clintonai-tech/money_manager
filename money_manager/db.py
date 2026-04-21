from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "money_manager.sqlite"


DEFAULT_CATEGORIES = [
    "Income",
    "Housing/Rent",
    "Utilities",
    "Groceries",
    "Restaurants/Cafes",
    "Transport",
    "Travel",
    "Shopping",
    "Household",
    "Health/Pharmacy",
    "Subscriptions/Telecom",
    "Gifts/Charity/Church",
    "Fees",
    "Transfers/Internal",
    "Refunds",
    "Uncategorized",
]


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[override]
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    bank_name TEXT NOT NULL,
    account_name TEXT NOT NULL,
    iban_masked TEXT,
    owner_label TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bank_name, account_name, iban_masked)
);

CREATE TABLE IF NOT EXISTS statement_imports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_filename TEXT NOT NULL,
    source_hash TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    statement_period_start TEXT,
    statement_period_end TEXT,
    opening_balance_cents INTEGER,
    closing_balance_cents INTEGER,
    row_count INTEGER NOT NULL DEFAULT 0,
    raw_metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS payment_instruments (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    instrument_type TEXT NOT NULL DEFAULT 'card',
    suffix TEXT NOT NULL,
    owner_label TEXT,
    first_seen TEXT,
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, instrument_type, suffix)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    import_id INTEGER NOT NULL REFERENCES statement_imports(id) ON DELETE CASCADE,
    booking_date TEXT NOT NULL,
    value_date TEXT NOT NULL,
    raw_party TEXT NOT NULL,
    normalized_merchant TEXT NOT NULL,
    booking_text TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    balance_cents INTEGER,
    direction TEXT NOT NULL CHECK(direction IN ('income', 'expense', 'zero')),
    payment_instrument_id INTEGER REFERENCES payment_instruments(id),
    raw_row_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_transactions_booking_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(normalized_merchant);
CREATE INDEX IF NOT EXISTS idx_transactions_direction ON transactions(direction);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transaction_classifications (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    confidence REAL NOT NULL DEFAULT 0,
    source_method TEXT NOT NULL,
    reviewer_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(reviewer_status IN ('pending', 'confirmed', 'manual')),
    rationale TEXT,
    rule_id INTEGER REFERENCES classification_rules(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS classification_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_merchant_pattern TEXT NOT NULL,
    purpose_pattern TEXT,
    direction TEXT CHECK(direction IN ('income', 'expense', 'zero')),
    min_amount_cents INTEGER,
    max_amount_cents INTEGER,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    priority INTEGER NOT NULL DEFAULT 100,
    confidence REAL NOT NULL DEFAULT 0.99,
    active INTEGER NOT NULL DEFAULT 1,
    created_from_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_classification_rules_active
    ON classification_rules(active, priority);

CREATE TABLE IF NOT EXISTS llm_classification_runs (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    masked_input_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    confidence REAL,
    token_count INTEGER,
    estimated_cost_cents INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS manual_review_events (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    previous_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    new_category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    rule_id INTEGER REFERENCES classification_rules(id) ON DELETE SET NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    seed_categories(conn)
    retire_cash_withdrawal_category(conn)
    conn.commit()


def seed_categories(conn: sqlite3.Connection) -> None:
    for sort_order, name in enumerate(DEFAULT_CATEGORIES, start=10):
        conn.execute(
            """
            INSERT INTO categories(name, sort_order)
            VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET
                sort_order = excluded.sort_order,
                active = 1
            """,
            (name, sort_order),
        )


def category_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown category: {name}")
    return int(row["id"])


def retire_cash_withdrawal_category(conn: sqlite3.Connection) -> None:
    cash = conn.execute("SELECT id FROM categories WHERE name = 'Cash Withdrawal'").fetchone()
    if cash is None:
        return
    travel_id = category_id(conn, "Travel")
    cash_id = int(cash["id"])
    conn.execute(
        "UPDATE transaction_classifications SET category_id = ? WHERE category_id = ?",
        (travel_id, cash_id),
    )
    conn.execute(
        "UPDATE classification_rules SET category_id = ?, active = 0 WHERE category_id = ?",
        (travel_id, cash_id),
    )
    conn.execute("UPDATE categories SET active = 0 WHERE id = ?", (cash_id,))


def cents_to_money(value: int | None) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def money_to_cents(text: str) -> int:
    clean = text.strip().replace(".", "").replace(",", ".")
    return int((Decimal(clean) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
