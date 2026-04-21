# Money Manager

Local-first ING joint-account dashboard with SQLite storage, deterministic rules,
manual review memory, and optional masked OpenAI classification.

## Quick Start

```bash
python3 -m money_manager init
python3 -m money_manager import /path/to/your/ing-statement.csv --classify
python3 -m money_manager web
```

Then open:

```text
http://127.0.0.1:8765
```

The executable wrapper supports the planned command shape too:

```bash
./money-manager import /path/to/your/ing-statement.csv
./money-manager classify
./money-manager web
./money-manager export
```

## Data Model

The SQLite database lives at:

```text
data/money_manager.sqlite
```

Important tables:

- `accounts`: masked account metadata.
- `statement_imports`: source file hash, period, balances, row count.
- `transactions`: normalized ledger rows with exact fingerprints for deduping.
- `payment_instruments`: locally detected card suffixes that can be labelled.
- `categories`: editable category taxonomy.
- `transaction_classifications`: current category and review status.
- `classification_rules`: reusable rules learned from manual review.
- `llm_classification_runs`: masked OpenAI request/response audit log.
- `manual_review_events`: manual classification audit trail.

## Classification

Run:

```bash
python3 -m money_manager classify
```

Classification order:

1. Saved manual rules.
2. Built-in household finance rules.
3. OpenAI API for unresolved transactions if `OPENAI_API_KEY` is set.
4. Manual review queue for anything uncertain.

To disable OpenAI even when an API key is set:

```bash
python3 -m money_manager classify --no-llm
```

The LLM payload masks reference-like tokens, card suffixes, IBAN-like strings,
ARNs, long numbers, and exact purchase dates. Full statements are never sent.

## Manual Review

Open `/review` in the local web app. Confirm a transaction category and leave
`Save as rule` checked to teach future imports. The app stores a reusable
merchant rule and applies it automatically on the next classification run.

Use `/categories` to add or rename local categories.

## Export

```bash
python3 -m money_manager export
```

This writes:

```text
exports/transactions.csv
```

## Backups

The app is local-first. Back up your database by copying:

```text
data/money_manager.sqlite
```

## Privacy

This repository is intended to contain code only. Local statement CSVs,
SQLite databases, exports, environment files, caches, and OS metadata are
ignored by `.gitignore`.
