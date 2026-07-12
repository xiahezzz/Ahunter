import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from advisor.db.migrate import migrate_database
from advisor.ledger.importer import import_ledger_csv, load_ledger_csv, main
from advisor.ledger.model import LedgerTransaction, apply_transactions


AS_OF = datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def write_ledger(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "transaction_id,trade_date,transaction_type,code,quantity,price,amount,fees\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    return path


def query_all(db_path: Path, sql: str):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def seed_market_prices(db_path: Path) -> None:
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.executemany(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount, source,
          fetched_at, as_of_date, content_hash, quality_status
        ) VALUES ('600519', ?, 100, 120, 90, ?, 1000, 10000, 'fixture', ?, ?, ?, ?)
        """,
        [
            ("2026-07-10", 110, AS_OF.isoformat(), "2026-07-10", "passed-close", "passed"),
            ("2026-07-11", 999, AS_OF.isoformat(), "2026-07-11", "failed-close", "failed"),
            ("2026-07-13", 888, AS_OF.isoformat(), "2026-07-13", "future-close", "passed"),
        ],
    )
    connection.commit()
    connection.close()


def test_apply_buy_and_sell_transactions():
    state = apply_transactions(
        [
            LedgerTransaction("t1", "2026-07-10", "cash_deposit", None, 0, 0, 100000, 0),
            LedgerTransaction("t2", "2026-07-10", "buy", "600519", 100, 100.0, -10000, 5),
            LedgerTransaction("t3", "2026-07-11", "sell", "600519", 100, 110.0, 11000, 5),
        ]
    )
    assert state.cash == 100990
    assert state.positions == {}
    assert state.realized_pnl == 990


def test_load_ledger_csv(tmp_path: Path):
    filename = tmp_path / "ledger.csv"
    filename.write_text(
        "transaction_id,trade_date,transaction_type,code,quantity,price,amount,fees\n"
        "t1,2026-07-10,cash_deposit,,0,0,100000,0\n",
        encoding="utf-8",
    )
    transactions = load_ledger_csv(filename)
    assert transactions[0].transaction_type == "cash_deposit"
    assert transactions[0].amount == 100000


def test_import_ledger_csv_persists_account_transactions_positions_and_snapshot(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    seed_market_prices(db_path)
    csv_path = write_ledger(
        tmp_path / "ledger.csv",
        [
            "deposit,2026-07-09,cash_deposit,,0,0,20000,0",
            "buy-1,2026-07-10,buy,600519,100,100,-10000,5",
        ],
    )

    result = import_ledger_csv(
        csv_path,
        db_path,
        account_id="broker-a",
        account_name="Broker A",
        source="broker_csv",
        as_of=AS_OF,
    )

    assert result.imported_count == 2
    assert query_all(db_path, "SELECT account_id, name FROM ledger_accounts") == [
        ("broker-a", "Broker A")
    ]
    assert query_all(
        db_path,
        "SELECT transaction_id, source FROM ledger_transactions ORDER BY trade_date",
    ) == [("deposit", "broker_csv"), ("buy-1", "broker_csv")]
    assert query_all(
        db_path,
        "SELECT account_id, code, quantity, cost_basis FROM positions",
    ) == [("broker-a", "600519", 100, 10005.0)]
    snapshot = query_all(
        db_path,
        "SELECT snapshot_id, cash, market_value, realized_pnl, unrealized_pnl, exposure_json "
        "FROM portfolio_snapshots",
    )[0]
    assert snapshot[0] == result.snapshot_id
    assert snapshot[1:5] == (9995.0, 11000.0, 0.0, 995.0)
    exposure = json.loads(snapshot[5])
    assert exposure["positions"]["600519"] == {
        "cost_basis": 10005.0,
        "market_price": 110.0,
        "market_value": 11000.0,
        "pricing_status": "passed",
        "quantity": 100,
        "unrealized_pnl": 995.0,
    }


def test_invalid_import_rolls_back_every_ledger_row(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    csv_path = write_ledger(
        tmp_path / "invalid.csv",
        [
            "deposit,2026-07-09,cash_deposit,,0,0,20000,0",
            "bad-buy,2026-07-10,buy,600519,100,100,10000,5",
        ],
    )

    with pytest.raises(ValueError, match="buy amount must be negative"):
        import_ledger_csv(csv_path, db_path, as_of=AS_OF)

    for table in ("ledger_accounts", "ledger_transactions", "positions", "portfolio_snapshots"):
        assert query_all(db_path, f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_existing_transaction_id_rolls_back_new_rows(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    first = write_ledger(
        tmp_path / "first.csv",
        ["deposit,2026-07-09,cash_deposit,,0,0,20000,0"],
    )
    import_ledger_csv(first, db_path, as_of=AS_OF)
    duplicate = write_ledger(
        tmp_path / "duplicate.csv",
        [
            "new-deposit,2026-07-10,cash_deposit,,0,0,1000,0",
            "deposit,2026-07-11,cash_deposit,,0,0,500,0",
        ],
    )

    with pytest.raises(ValueError, match="duplicate transaction id"):
        import_ledger_csv(duplicate, db_path, as_of=AS_OF)

    assert query_all(
        db_path, "SELECT transaction_id FROM ledger_transactions ORDER BY transaction_id"
    ) == [("deposit",)]
    assert query_all(db_path, "SELECT COUNT(*) FROM portfolio_snapshots") == [(1,)]


def test_cli_resolves_config_database_and_prints_json_status(tmp_path: Path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "advisor.yaml"
    config_path.write_text(
        """
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  database: state/advisor.sqlite
data_sources:
  allow_tushare: false
  free_sources: []
""".strip(),
        encoding="utf-8",
    )
    csv_path = write_ledger(
        tmp_path / "ledger.csv",
        ["deposit,2026-07-09,cash_deposit,,0,0,20000,0"],
    )

    main([
        str(csv_path),
        "--config", str(config_path),
        "--account-id", "cli-account",
        "--account-name", "CLI Account",
        "--source", "cli_csv",
        "--as-of", AS_OF.isoformat(),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "imported"
    assert payload["imported_count"] == 1
    assert "secret" not in json.dumps(payload).lower()
    assert query_all(tmp_path / "state" / "advisor.sqlite", "SELECT account_id FROM ledger_accounts") == [
        ("cli-account",)
    ]


def test_apply_fee_transaction_adjusts_cash():
    state = apply_transactions(
        [
            LedgerTransaction("t1", "2026-07-10", "cash_deposit", None, 0, 0, 100000, 0),
            LedgerTransaction("t2", "2026-07-10", "fee", None, 0, 0, -15, 0),
        ]
    )
    assert state.cash == 99985
    assert state.positions == {}
    assert state.realized_pnl == 0


def test_apply_tax_transaction_adjusts_cash():
    state = apply_transactions(
        [
            LedgerTransaction("t1", "2026-07-10", "cash_deposit", None, 0, 0, 100000, 0),
            LedgerTransaction("t2", "2026-07-10", "tax", None, 0, 0, -25, 0),
        ]
    )
    assert state.cash == 99975
    assert state.positions == {}
    assert state.realized_pnl == 0


def test_buy_without_code_raises_value_error():
    with pytest.raises(ValueError, match="buy transaction requires code"):
        apply_transactions(
            [
                LedgerTransaction("t1", "2026-07-10", "buy", None, 100, 100.0, -10000, 5),
            ]
        )


def test_sell_without_code_raises_value_error():
    with pytest.raises(ValueError, match="sell transaction requires code"):
        apply_transactions(
            [
                LedgerTransaction("t1", "2026-07-10", "sell", None, 100, 110.0, 11000, 5),
            ]
        )
