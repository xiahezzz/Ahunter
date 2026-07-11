from pathlib import Path

from advisor.ledger.importer import load_ledger_csv
from advisor.ledger.model import LedgerTransaction, apply_transactions


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
