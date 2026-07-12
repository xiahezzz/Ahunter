import math
import re
from dataclasses import dataclass, field
from datetime import date


_CODE_RE = re.compile(r"[03468]\d{5}\Z")
_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_SQLITE_INTEGER = 2**63 - 1
_TRANSACTION_TYPE_PRIORITY = {
    "cash_deposit": 0,
    "cash_withdrawal": 0,
    "buy": 1,
    "sell": 2,
    "fee": 3,
    "tax": 3,
}


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    trade_date: str
    transaction_type: str
    code: str | None
    quantity: int
    price: float
    amount: float
    fees: float


@dataclass
class LedgerState:
    cash: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    cost_basis: dict[str, float] = field(default_factory=dict)
    realized_pnl: float = 0.0


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if isinstance(value, int) and not -_MAX_SQLITE_INTEGER <= value <= _MAX_SQLITE_INTEGER:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def validate_ledger_transaction(transaction: LedgerTransaction) -> None:
    if not isinstance(transaction.transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(
        transaction.transaction_id
    ):
        raise ValueError("invalid transaction id")
    try:
        if date.fromisoformat(transaction.trade_date).isoformat() != transaction.trade_date:
            raise ValueError("invalid trade date")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid trade date") from error
    if transaction.transaction_type not in {
        "cash_deposit", "cash_withdrawal", "buy", "sell", "fee", "tax"
    }:
        raise ValueError("invalid transaction type")
    if (
        not isinstance(transaction.quantity, int)
        or isinstance(transaction.quantity, bool)
        or not 0 <= transaction.quantity <= _MAX_SQLITE_INTEGER
    ):
        raise ValueError("invalid quantity")
    if not all(_finite_number(value) for value in (transaction.price, transaction.amount, transaction.fees)):
        raise ValueError("invalid numeric amount")
    if transaction.transaction_type in {"buy", "sell"}:
        if not isinstance(transaction.code, str) or not _CODE_RE.fullmatch(transaction.code):
            raise ValueError("trade requires valid stock code")
        if transaction.quantity <= 0 or transaction.price <= 0 or transaction.fees < 0:
            raise ValueError("invalid trade quantity, price, or fees")
        if transaction.transaction_type == "buy" and transaction.amount >= 0:
            raise ValueError("buy amount must be negative")
        if transaction.transaction_type == "sell" and transaction.amount <= 0:
            raise ValueError("sell amount must be positive")
        if not math.isclose(
            abs(transaction.amount),
            transaction.quantity * transaction.price,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError("trade amount does not match quantity and price")
    elif (
        transaction.code is not None
        or transaction.quantity != 0
        or transaction.price != 0
        or transaction.fees != 0
    ):
        raise ValueError("invalid cash transaction")
    elif transaction.transaction_type == "cash_deposit" and transaction.amount <= 0:
        raise ValueError("cash deposit must be positive")
    elif transaction.transaction_type in {"cash_withdrawal", "fee", "tax"} and transaction.amount >= 0:
        raise ValueError("cash outflow must be negative")


def validate_ledger_state(state: LedgerState) -> None:
    if not all(
        _finite_number(value)
        for value in (state.cash, state.realized_pnl, *state.cost_basis.values())
    ):
        raise ValueError("non-finite ledger state")


def _require_code(tx: LedgerTransaction) -> str:
    if tx.code is None:
        raise ValueError(f"{tx.transaction_type} transaction requires code")
    return tx.code


def ledger_transaction_sort_key(transaction: LedgerTransaction) -> tuple[str, int, str]:
    return (
        transaction.trade_date,
        _TRANSACTION_TYPE_PRIORITY.get(transaction.transaction_type, 4),
        transaction.transaction_id,
    )


def apply_transactions(transactions: list[LedgerTransaction]) -> LedgerState:
    state = LedgerState()
    for tx in transactions:
        if tx.transaction_type in {"cash_deposit", "cash_withdrawal", "fee", "tax"}:
            state.cash += tx.amount
        elif tx.transaction_type == "buy":
            code = _require_code(tx)
            state.cash += tx.amount - tx.fees
            state.positions[code] = state.positions.get(code, 0) + tx.quantity
            state.cost_basis[code] = state.cost_basis.get(code, 0.0) + abs(tx.amount) + tx.fees
        elif tx.transaction_type == "sell":
            code = _require_code(tx)
            held = state.positions.get(code, 0)
            if held < tx.quantity:
                raise ValueError(f"cannot sell {tx.quantity} shares of {code}; only {held} held")
            prior_cost = state.cost_basis.get(code, 0.0)
            sold_cost = prior_cost * (tx.quantity / held)
            state.cash += tx.amount - tx.fees
            state.realized_pnl += tx.amount - tx.fees - sold_cost
            remaining = held - tx.quantity
            if remaining == 0:
                state.positions.pop(code, None)
                state.cost_basis.pop(code, None)
            else:
                state.positions[code] = remaining
                state.cost_basis[code] = prior_cost - sold_cost
        else:
            raise ValueError(f"unsupported transaction_type: {tx.transaction_type}")
    validate_ledger_state(state)
    return state
