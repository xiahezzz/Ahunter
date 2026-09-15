"""Cumulative per-order charges from pinned fee evidence and broker assumptions."""
from decimal import Decimal, ROUND_HALF_UP

from .contracts import ExecutionSettings
from .data.contracts import FeeSchedule


class FeeUnavailable(ValueError):
    pass


def rounded(value, quantum):
    value, quantum = Decimal(value), Decimal(quantum)
    if not value.is_finite() or not quantum.is_finite() or quantum <= 0:
        raise ValueError("finite amount and positive quantum required")
    return (value / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum


class OrderFees:
    def __init__(self, execution, schedule, *, trade_date, exchange):
        self.execution = ExecutionSettings.model_validate(execution)
        self.schedule = FeeSchedule.model_validate(schedule)
        if self.schedule.exchange != exchange or not self.schedule.effective_from <= trade_date <= self.schedule.effective_to:
            raise FeeUnavailable("fee schedule does not cover the market/date")
        if any(item.fee_id == "commission" for item in self.schedule.items):
            raise FeeUnavailable("broker commission must be separate from statutory items")
        if self.execution.fee_quantum != Decimal("0.01") or any(item.quantum != Decimal("0.01") for item in self.schedule.items):
            raise FeeUnavailable("cash_equity_v1 requires currency-cent fee rounding")
        if any(item.included_in_commission and not self.execution.commission_includes_exchange_fees for item in self.schedule.items):
            raise FeeUnavailable("fee inclusion conflicts with broker commission assumptions")
        if any(item.fee_id in {"exchange_fee", "handling_fee", "regulatory_fee"} and not item.included_in_commission
               and self.execution.commission_includes_exchange_fees for item in self.schedule.items):
            raise FeeUnavailable("exchange fee inclusion must match the commission assumption")
        if any(item.included_in_commission and item.fee_id in {"stamp_duty", "transfer_fee"} for item in self.schedule.items):
            raise FeeUnavailable("statutory tax/transfer charge cannot be hidden inside commission")

    def cumulative(self, side, turnover):
        turnover = Decimal(turnover)
        if side not in {"buy", "sell"} or not turnover.is_finite() or turnover < 0:
            raise ValueError("valid side and nonnegative finite turnover required")
        if turnover == 0:
            return {"commission": Decimal("0"), **{item.fee_id: Decimal("0") for item in self.schedule.items}}
        result = {"commission": rounded(max(turnover * self.execution.commission_rate, self.execution.minimum_commission), self.execution.fee_quantum)}
        for item in self.schedule.items:
            result[item.fee_id] = (Decimal("0") if item.included_in_commission or item.side not in {"both", side}
                                   else rounded(max(turnover * item.rate, item.minimum), item.quantum))
        return result

    def incremental(self, side, before, after):
        if Decimal(after) < Decimal(before):
            raise ValueError("cumulative turnover cannot decrease")
        prior, current = self.cumulative(side, before), self.cumulative(side, after)
        return {key: current[key] - prior[key] for key in current}

    def total(self, side, turnover):
        return sum(self.cumulative(side, turnover).values(), Decimal("0"))
