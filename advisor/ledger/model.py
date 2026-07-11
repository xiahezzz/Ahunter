from dataclasses import dataclass, field


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


def apply_transactions(transactions: list[LedgerTransaction]) -> LedgerState:
    state = LedgerState()
    for tx in transactions:
        if tx.transaction_type in {"cash_deposit", "cash_withdrawal"}:
            state.cash += tx.amount
        elif tx.transaction_type == "buy":
            assert tx.code is not None
            state.cash += tx.amount - tx.fees
            state.positions[tx.code] = state.positions.get(tx.code, 0) + tx.quantity
            state.cost_basis[tx.code] = state.cost_basis.get(tx.code, 0.0) + abs(tx.amount) + tx.fees
        elif tx.transaction_type == "sell":
            assert tx.code is not None
            held = state.positions.get(tx.code, 0)
            if held < tx.quantity:
                raise ValueError(f"cannot sell {tx.quantity} shares of {tx.code}; only {held} held")
            prior_cost = state.cost_basis.get(tx.code, 0.0)
            sold_cost = prior_cost * (tx.quantity / held)
            state.cash += tx.amount - tx.fees
            state.realized_pnl += tx.amount - tx.fees - sold_cost
            remaining = held - tx.quantity
            if remaining == 0:
                state.positions.pop(tx.code, None)
                state.cost_basis.pop(tx.code, None)
            else:
                state.positions[tx.code] = remaining
                state.cost_basis[tx.code] = prior_cost - sold_cost
        else:
            raise ValueError(f"unsupported transaction_type: {tx.transaction_type}")
    return state
