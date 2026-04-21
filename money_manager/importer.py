from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import db


ING_COLUMNS = [
    "booking_date_de",
    "value_date_de",
    "raw_party",
    "booking_text",
    "purpose",
    "balance",
    "balance_currency",
    "amount",
    "currency",
]


@dataclass(frozen=True)
class ImportResult:
    source_file: Path
    account_id: int
    import_id: int
    rows_seen: int
    inserted: int
    skipped_duplicates: int
    period_start: str | None
    period_end: str | None


def import_ing_csv(path: str | Path, db_path: str | Path = db.DEFAULT_DB_PATH) -> ImportResult:
    source = Path(path)
    raw_bytes = source.read_bytes()
    text = decode_statement(raw_bytes)
    source_hash = hashlib.sha256(raw_bytes).hexdigest()

    metadata, rows = parse_ing_csv(text)
    period_start, period_end = parse_period(metadata.get("Zeitraum"))

    with db.connect(db_path) as conn:
        db.init_db(conn)
        account_id = ensure_account(conn, metadata)
        closing_balance = db.money_to_cents(metadata["Saldo"]) if metadata.get("Saldo") else None
        opening_balance = infer_opening_balance(rows)

        import_id = ensure_statement_import(
            conn=conn,
            account_id=account_id,
            source_filename=str(source),
            source_hash=source_hash,
            period_start=period_start,
            period_end=period_end,
            opening_balance_cents=opening_balance,
            closing_balance_cents=closing_balance,
            row_count=len(rows),
            metadata=metadata,
        )

        inserted = 0
        skipped = 0
        for row in rows:
            if insert_transaction(conn, account_id, import_id, row):
                inserted += 1
            else:
                skipped += 1

        conn.commit()

    return ImportResult(
        source_file=source,
        account_id=account_id,
        import_id=import_id,
        rows_seen=len(rows),
        inserted=inserted,
        skipped_duplicates=skipped,
        period_start=period_start,
        period_end=period_end,
    )


def decode_statement(raw_bytes: bytes) -> str:
    for encoding in ("cp1252", "iso-8859-1", "utf-8-sig", "utf-8"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("cp1252", errors="replace")


def parse_ing_csv(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    lines = text.splitlines()
    header_idx = next(
        (idx for idx, line in enumerate(lines) if line.startswith("Buchung;Wertstellungsdatum;")),
        None,
    )
    if header_idx is None:
        raise ValueError("Could not find ING transaction header row")

    metadata: dict[str, str] = {}
    for line in lines[:header_idx]:
        if not line.strip() or ";" not in line:
            continue
        parts = next(csv.reader([line], delimiter=";"))
        key = parts[0].strip()
        values = [part.strip() for part in parts[1:] if part.strip()]
        if key and values:
            metadata[key] = values[0]

    parsed_rows: list[dict[str, str]] = []
    reader = csv.reader(lines[header_idx + 1 :], delimiter=";")
    for values in reader:
        if not values or all(not value.strip() for value in values):
            continue
        if len(values) != len(ING_COLUMNS):
            raise ValueError(f"Unexpected ING row length {len(values)}: {values}")
        parsed_rows.append(dict(zip(ING_COLUMNS, (value.strip() for value in values), strict=True)))

    return metadata, parsed_rows


def parse_period(text: str | None) -> tuple[str | None, str | None]:
    if not text or " - " not in text:
        return None, None
    start_de, end_de = text.split(" - ", 1)
    return parse_de_date(start_de), parse_de_date(end_de)


def parse_de_date(text: str) -> str:
    return datetime.strptime(text.strip(), "%d.%m.%Y").date().isoformat()


def infer_opening_balance(rows: list[dict[str, str]]) -> int | None:
    if not rows:
        return None
    oldest = rows[-1]
    return db.money_to_cents(oldest["balance"]) - db.money_to_cents(oldest["amount"])


def mask_iban(iban: str | None) -> str | None:
    if not iban:
        return None
    compact = re.sub(r"\s+", "", iban)
    if len(compact) < 8:
        return "****"
    return f"{compact[:4]} **** **** **** {compact[-4:]}"


def ensure_account(conn, metadata: dict[str, str]) -> int:
    bank = metadata.get("Bank") or "ING"
    account_name = metadata.get("Kontoname") or "Account"
    iban_masked = mask_iban(metadata.get("IBAN"))
    owner_label = metadata.get("Kunde")
    conn.execute(
        """
        INSERT INTO accounts(bank_name, account_name, iban_masked, owner_label)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(bank_name, account_name, iban_masked) DO UPDATE SET
            owner_label = COALESCE(excluded.owner_label, accounts.owner_label)
        """,
        (bank, account_name, iban_masked, owner_label),
    )
    row = conn.execute(
        """
        SELECT id FROM accounts
        WHERE bank_name = ? AND account_name = ? AND iban_masked IS ?
        """,
        (bank, account_name, iban_masked),
    ).fetchone()
    return int(row["id"])


def ensure_statement_import(
    conn,
    account_id: int,
    source_filename: str,
    source_hash: str,
    period_start: str | None,
    period_end: str | None,
    opening_balance_cents: int | None,
    closing_balance_cents: int | None,
    row_count: int,
    metadata: dict[str, str],
) -> int:
    conn.execute(
        """
        INSERT INTO statement_imports(
            account_id, source_filename, source_hash, statement_period_start,
            statement_period_end, opening_balance_cents, closing_balance_cents,
            row_count, raw_metadata_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_hash) DO NOTHING
        """,
        (
            account_id,
            source_filename,
            source_hash,
            period_start,
            period_end,
            opening_balance_cents,
            closing_balance_cents,
            row_count,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute(
        "SELECT id FROM statement_imports WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    return int(row["id"])


def insert_transaction(conn, account_id: int, import_id: int, row: dict[str, str]) -> bool:
    booking_date = parse_de_date(row["booking_date_de"])
    value_date = parse_de_date(row["value_date_de"])
    amount_cents = db.money_to_cents(row["amount"])
    balance_cents = db.money_to_cents(row["balance"])
    direction = "income" if amount_cents > 0 else "expense" if amount_cents < 0 else "zero"
    merchant = normalize_merchant(row["raw_party"])
    instrument_id = ensure_payment_instrument(conn, account_id, booking_date, row["purpose"])
    fingerprint = transaction_fingerprint(row, amount_cents, balance_cents)

    cursor = conn.execute(
        """
        INSERT INTO transactions(
            account_id, import_id, booking_date, value_date, raw_party,
            normalized_merchant, booking_text, purpose, amount_cents,
            currency, balance_cents, direction, payment_instrument_id,
            raw_row_json, fingerprint
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, fingerprint) DO NOTHING
        """,
        (
            account_id,
            import_id,
            booking_date,
            value_date,
            row["raw_party"],
            merchant,
            row["booking_text"],
            row["purpose"],
            amount_cents,
            row["currency"],
            balance_cents,
            direction,
            instrument_id,
            json.dumps(row, ensure_ascii=False, sort_keys=True),
            fingerprint,
        ),
    )
    return cursor.rowcount == 1


def ensure_payment_instrument(conn, account_id: int, seen_date: str, purpose: str) -> int | None:
    match = re.search(r"\bNR\s+XXXX\s+(\d{4})\b", purpose)
    if not match:
        return None
    suffix = match.group(1)
    conn.execute(
        """
        INSERT INTO payment_instruments(account_id, instrument_type, suffix, first_seen, last_seen)
        VALUES(?, 'card', ?, ?, ?)
        ON CONFLICT(account_id, instrument_type, suffix) DO UPDATE SET
            first_seen = MIN(COALESCE(payment_instruments.first_seen, excluded.first_seen), excluded.first_seen),
            last_seen = MAX(COALESCE(payment_instruments.last_seen, excluded.last_seen), excluded.last_seen)
        """,
        (account_id, suffix, seen_date, seen_date),
    )
    row = conn.execute(
        """
        SELECT id FROM payment_instruments
        WHERE account_id = ? AND instrument_type = 'card' AND suffix = ?
        """,
        (account_id, suffix),
    ).fetchone()
    return int(row["id"])


def transaction_fingerprint(row: dict[str, str], amount_cents: int, balance_cents: int) -> str:
    parts = [
        row["booking_date_de"],
        row["value_date_de"],
        row["raw_party"],
        row["booking_text"],
        row["purpose"],
        str(amount_cents),
        row["currency"],
        str(balance_cents),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalize_merchant(raw_party: str) -> str:
    merchant = raw_party.upper().strip()
    merchant = re.sub(r"^VISA\s+", "", merchant)
    merchant = re.sub(r"\s+", " ", merchant)
    merchant = re.sub(r"\s+\d{3,}$", "", merchant)
    return merchant.strip()
