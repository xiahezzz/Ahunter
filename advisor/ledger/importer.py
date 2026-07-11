import argparse
import csv
from pathlib import Path

from advisor.ledger.model import LedgerTransaction


def load_ledger_csv(path: Path) -> list[LedgerTransaction]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            LedgerTransaction(
                transaction_id=row["transaction_id"],
                trade_date=row["trade_date"],
                transaction_type=row["transaction_type"],
                code=row["code"] or None,
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                amount=float(row["amount"]),
                fees=float(row["fees"]),
            )
            for row in rows
        ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    args = parser.parse_args()
    transactions = load_ledger_csv(Path(args.csv_path))
    print(f"loaded {len(transactions)} ledger transactions")
