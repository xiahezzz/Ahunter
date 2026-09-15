"""Corporate entitlements and ordinary-share dividend tax, inside the Test ledger.

Inputs are host-sealed historical terms. No built-in statutory rate or issuer
schedule is inferred; unavailable policy and fractional allocation fail closed.
"""
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .contracts import Contract, Name, Hash, Money, PositiveInt, numeric_field
from .data.contracts import CorporateAction, MarketRule
from .data.temporal import Availability, Boundary, aware
from .fees import rounded
from .account import RESOURCE_HELD_ORDER_STATES, AccountUnavailable, dec, ZERO


class CorporateUnsupported(AccountUnavailable):
    pass


def add_months(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


class TaxBand(Contract):
    up_to_months: PositiveInt | None = numeric_field("calendar_month", null="no upper holding-period limit")
    rate: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)] = numeric_field("fraction_of_taxable_dividend")


class DividendTaxPolicy(Contract):
    policy_ref: Name
    effective_from: date
    effective_to: date
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    availability: Availability
    scope: Literal["ordinary_public_a_share"]
    holding_period: Literal["calendar_month_acquisition_through_day_before_disposal"]
    disposal_order: Literal["fifo"]
    withholding: Literal["distribution_or_later_disposal"]
    bands: tuple[TaxBand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        limits = [band.up_to_months for band in self.bands]
        if (self.effective_to < self.effective_from or limits[-1] is not None or None in limits[:-1]
                or limits[:-1] != sorted(set(limits[:-1]))):
            raise ValueError("tax policy needs ordered finite bands and a final unlimited band")
        return self

    def band_index(self, acquired_on, disposed_on):
        if disposed_on < acquired_on:
            raise CorporateUnsupported("dividend tax cannot precede acquisition")
        return next(index for index, band in enumerate(self.bands)
                    if band.up_to_months is None or disposed_on <= add_months(acquired_on, band.up_to_months))

    def rate(self, acquired_on, disposed_on):
        return self.bands[self.band_index(acquired_on, disposed_on)].rate

    def reserve_rate(self, acquired_on, as_of):
        # Maximum still applicable from today's holding period onward; no future
        # disposal date may be chosen to lower the outstanding reservation.
        return max(band.rate for band in self.bands[self.band_index(acquired_on, as_of):])


class CorporateTerms(Contract):
    security: Name
    action: CorporateAction
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    availability: Availability
    market_rule: MarketRule
    record_at: datetime
    effective_at: datetime
    payment_at: datetime | None
    tradable_at: datetime | None
    taxable_per_old_share: Money = numeric_field("CNY_per_record_date_share")
    tax_policy: DividendTaxPolicy | None
    new_share_holding_start: Literal["inherit", "effective_date"] | None
    fractional_allocation: Literal["exact_integer_required"]

    @model_validator(mode="after")
    def coherent(self):
        action = self.action
        for instant in (self.record_at, self.effective_at, self.payment_at, self.tradable_at):
            if instant is not None:
                aware(instant)
        if self.record_at.date() != action.record_date or self.effective_at.date() != action.effective_date or self.effective_at <= self.record_at:
            raise ValueError("corporate event dates or order disagree with terms")
        if ((self.payment_at.date() if self.payment_at else None) != action.payment_date
                or (self.tradable_at.date() if self.tradable_at else None) != action.tradable_date):
            raise ValueError("payment/tradability dates disagree with terms")
        if self.payment_at is not None and self.payment_at < self.effective_at:
            raise ValueError("payment cannot precede entitlement detachment")
        if self.tradable_at is not None and self.tradable_at < self.effective_at:
            raise ValueError("new shares cannot trade before effective date")
        if action.kind == "dividend" and (action.cash_per_share is None or self.payment_at is None or self.taxable_per_old_share != action.cash_per_share):
            raise ValueError("ordinary cash dividend needs full taxable cash and payment terms")
        if action.kind in {"bonus", "split"} and (action.shares_ratio is None or action.shares_ratio <= 0 or self.tradable_at is None or self.new_share_holding_start is None):
            raise ValueError("share event needs ratio, tradability and holding-period terms")
        if self.taxable_per_old_share > 0 and self.tax_policy is None:
            raise ValueError("taxable entitlement lacks its historical tax policy")
        return self


def refresh_tax_reserves(state, at):
    total = ZERO
    for obligation in state.get("dividend_obligations", {}).values():
        policy = DividendTaxPolicy.model_validate(obligation["policy"])
        if not policy.effective_from <= at.date() <= policy.effective_to:
            raise CorporateUnsupported("dividend tax policy does not cover this accounting date")
        reserve = rounded(dec(obligation["basis_remaining"]) * policy.reserve_rate(date.fromisoformat(obligation["acquired_on"]), at.date()), Decimal("0.01"))
        obligation["reserved"] = str(reserve)
        total += reserve
    previous = dec(state.get("dividend_tax_reserve", "0"))
    state["liabilities"] = str(dec(state["liabilities"]) + total - previous)
    state["dividend_tax_reserve"] = str(total)
    return {"account": "dividend_tax_reserve", "amount": str(total - previous)}


def assess_disposal(state, lot_id, quantity, at):
    """Return tax collectible at this sale; pre-distribution amounts stay payable."""
    collectible, assessed, entries = ZERO, ZERO, []
    for key, obligation in state.get("dividend_obligations", {}).items():
        if obligation["lot_id"] != lot_id or obligation["shares_remaining"] <= 0:
            continue
        if quantity > obligation["shares_remaining"]:
            raise CorporateUnsupported("tax entitlement units disagree with disposed shares")
        policy = DividendTaxPolicy.model_validate(obligation["policy"])
        if not policy.effective_from <= at.date() <= policy.effective_to:
            raise CorporateUnsupported("dividend tax policy does not cover this accounting date")
        basis = dec(obligation["basis_remaining"]) * quantity / obligation["shares_remaining"]
        exact = dec(obligation["tax_exact"]) + basis * policy.rate(date.fromisoformat(obligation["acquired_on"]), at.date())
        cumulative = rounded(exact, Decimal("0.01"))
        increment = cumulative - dec(obligation["assessed"])
        obligation.update(basis_remaining=str(dec(obligation["basis_remaining"]) - basis),
                          shares_remaining=obligation["shares_remaining"] - quantity,
                          tax_exact=str(exact), assessed=str(cumulative))
        action = state["corporate_actions"][obligation["action_id"]]
        if action["distributed"]:
            collectible += increment
        else:
            action["deferred_tax"] = str(dec(action["deferred_tax"]) + increment)
            state["deferred_dividend_tax"] = str(dec(state.get("deferred_dividend_tax", "0")) + increment)
            state["payables"] = str(dec(state["payables"]) + increment)
            entries.append({"account": "payables", "amount": str(increment), "reason": "dividend_tax_not_yet_distributed"})
        assessed += increment
        entries.append({"account": "dividend_tax_assessed", "amount": str(increment), "obligation_id": key})
    if assessed:
        state["fees_assessed"]["dividend_tax"] = str(dec(state["fees_assessed"].get("dividend_tax", "0")) + assessed)
    return collectible, entries


class CorporateActions:
    def __init__(self, account):
        self.account, self.records = account, account.records

    def register(self, terms, *, at, action_id):
        terms = CorporateTerms.model_validate(terms)
        at = aware(at).astimezone(ZoneInfo(self.account.spec.clock.timezone))
        action = terms.action
        request = {"operation": "corporate_register", "terms": terms.model_dump(mode="json"), "at": at.isoformat()}
        previous = self.account._prior(action_id, request)
        if previous is not None:
            return previous
        if action.kind in {"bonus", "split"} and terms.taxable_per_old_share:
            raise CorporateUnsupported("taxable share distributions need an explicit tax-unit allocation policy")
        if action.kind in {"delisting", "merger"}:
            raise CorporateUnsupported("special corporate events need executable settlement/conversion terms")
        if (at != terms.record_at or at.date() not in self.account.sessions or terms.security[:2] != terms.market_rule.exchange
                or not terms.market_rule.effective_from <= at.date() <= terms.market_rule.effective_to
                or at.timetz().replace(tzinfo=None) != terms.market_rule.closing_auction.end):
            raise CorporateUnsupported("entitlement registration must use the proven market record-date close")
        boundary = Boundary(event_cutoff=at, snapshot_at=at, market_timezone=self.account.spec.clock.timezone)
        if not terms.availability.visible(boundary):
            raise CorporateUnsupported("corporate terms were unavailable at registration")
        if terms.tax_policy is not None and (not terms.tax_policy.effective_from <= action.record_date <= terms.tax_policy.effective_to
                                            or not terms.tax_policy.availability.visible(boundary)):
            raise CorporateUnsupported("tax policy does not cover the entitlement registration")
        current = self.account._state()
        state = current["value"]
        if action.action_id in state["corporate_actions"]:
            raise CorporateUnsupported("corporate entitlement identity was already registered")
        entitlements = []
        for lot_id, lot in state["stock_lots"].items():
            if lot["security"] == terms.security and lot["quantity"]:
                entitlements.append({"lot_id": lot_id, "quantity": lot["quantity"], "acquired_on": lot["acquired_on"]})
        item = {"terms": terms.model_dump(mode="json"), "entitlements": entitlements, "applied": False,
                "distributed": False, "deferred_tax": "0", "status": "registered"}
        state["corporate_actions"][action.action_id] = item
        entries = []
        if action.kind == "rights":
            if self.account.spec.account.voluntary_subscription:
                raise CorporateUnsupported("voluntary subscription policy is unsupported")
            item.update(status="not_participating", applied=True, distributed=True)
        else:
            if action.kind == "dividend":
                amount = rounded(sum((entry["quantity"] * action.cash_per_share for entry in entitlements), ZERO), Decimal("0.01"))
                state["receivables"][action.action_id] = {"amount": str(amount), "qualified": True, "nav_amount": "0",
                    "kind": "dividend", "detached": False, "source_ref": terms.source_refs[0]}
                entries.append({"account": "dividend_entitlement", "amount": str(amount), "action_id": action.action_id})
            if terms.taxable_per_old_share:
                for entry in entitlements:
                    key = action.action_id + ":" + entry["lot_id"]
                    state.setdefault("dividend_obligations", {})[key] = {
                        "action_id": action.action_id, "lot_id": entry["lot_id"], "acquired_on": entry["acquired_on"],
                        "shares_remaining": entry["quantity"], "basis_remaining": str(entry["quantity"] * terms.taxable_per_old_share),
                        "tax_exact": "0", "assessed": "0", "reserved": "0", "policy": terms.tax_policy.model_dump(mode="json")}
        return self.account._commit(action_id, request, state, current["sequence"], at=at, visible_at=at, entries=entries)

    def apply(self, corporate_id, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.account.spec.clock.timezone))
        request = {"operation": "corporate_apply", "corporate_id": corporate_id, "at": at.isoformat()}
        previous = self.account._prior(action_id, request)
        if previous is not None:
            return previous
        current = self.account._state()
        state = current["value"]
        item = state["corporate_actions"].get(corporate_id)
        if item is None or item["applied"]:
            raise CorporateUnsupported("entitlement is absent or already applied")
        terms = CorporateTerms.model_validate(item["terms"])
        action = terms.action
        if at != terms.effective_at or at.date() not in self.account.sessions:
            raise CorporateUnsupported("corporate action requires its fixed effective instant/calendar")
        if any(order["security"] == terms.security and order["status"] in RESOURCE_HELD_ORDER_STATES for order in state["orders"].values()):
            raise CorporateUnsupported("unresolved orders must expire before corporate price/share adjustment")
        entries = []
        if action.kind == "dividend":
            receivable = state["receivables"][corporate_id]
            receivable.update(detached=True, nav_amount=receivable["amount"])
        elif action.kind in {"bonus", "split"}:
            if action.kind == "bonus" and any(obligation["lot_id"] in {entry["lot_id"] for entry in item["entitlements"]}
                    and dec(obligation["basis_remaining"]) > 0 for obligation in state.get("dividend_obligations", {}).values()):
                raise CorporateUnsupported("bonus shares need explicit outstanding dividend tax-unit allocation")
            for entry in item["entitlements"]:
                lot = state["stock_lots"][entry["lot_id"]]
                if lot["quantity"] != entry["quantity"]:
                    raise CorporateUnsupported("share action needs complete ownership at its effective boundary")
                target = dec(entry["quantity"]) * action.shares_ratio
                if target != target.to_integral_value():
                    raise CorporateUnsupported("fractional-share allocation evidence is unavailable")
                new_quantity = int(target)
                if action.kind == "split":
                    if new_quantity <= 0:
                        raise CorporateUnsupported("invalid split resulting quantity")
                    factor = Decimal(new_quantity) / entry["quantity"]
                    lot["quantity"] = new_quantity
                    lot["cost_basis"] = str(dec(lot["cost_basis"]) / factor)
                    lot["sellable_on"], lot["available_at"] = terms.tradable_at.date().isoformat(), terms.tradable_at.isoformat()
                    if terms.new_share_holding_start == "effective_date":
                        lot["acquired_on"] = at.date().isoformat()
                    for obligation in state.get("dividend_obligations", {}).values():
                        if obligation["lot_id"] == entry["lot_id"]:
                            units = obligation["shares_remaining"] * factor
                            if units != units.to_integral_value():
                                raise CorporateUnsupported("fractional tax-unit allocation is unavailable")
                            obligation["shares_remaining"] = int(units)
                    delta = new_quantity - entry["quantity"]
                else:
                    lot_id = "corporate:" + corporate_id + ":" + entry["lot_id"]
                    total = entry["quantity"] + new_quantity
                    basis = dec(lot["cost_basis"]) * entry["quantity"] / total
                    lot["cost_basis"] = str(basis)
                    state["stock_lots"][lot_id] = {"security": terms.security, "quantity": new_quantity,
                        "acquired_on": entry["acquired_on"] if terms.new_share_holding_start == "inherit" else at.date().isoformat(),
                        "acquired_sequence": state.get("next_lot_sequence", 0), "cost_basis": str(basis),
                        "sellable_on": terms.tradable_at.date().isoformat(), "available_at": terms.tradable_at.isoformat()}
                    state["next_lot_sequence"] = state.get("next_lot_sequence", 0) + 1
                    delta = new_quantity
                entries.append({"account": "shares", "security": terms.security, "quantity": delta, "action_id": corporate_id})
            item["distributed"] = True
        else:
            raise CorporateUnsupported("corporate action application is unsupported")
        item.update(applied=True, status="applied")
        return self.account._commit(action_id, request, state, current["sequence"], at=at, visible_at=at, entries=entries)

    def pay(self, corporate_id, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.account.spec.clock.timezone))
        request = {"operation": "corporate_pay", "corporate_id": corporate_id, "at": at.isoformat()}
        previous = self.account._prior(action_id, request)
        if previous is not None:
            return previous
        current = self.account._state()
        state = current["value"]
        item = state["corporate_actions"].get(corporate_id)
        if item is None or not item["applied"] or item["distributed"]:
            raise CorporateUnsupported("cash entitlement is absent, attached or already paid")
        terms = CorporateTerms.model_validate(item["terms"])
        if at != terms.payment_at or terms.action.kind != "dividend":
            raise CorporateUnsupported("payment requires its fixed distribution time")
        receivable = state["receivables"][corporate_id]
        gross, tax = dec(receivable["amount"]), dec(item["deferred_tax"])
        if tax > gross:
            raise CorporateUnsupported("cash dividend cannot cover its deferred dividend tax")
        state["payables"] = str(dec(state["payables"]) - tax)
        state["deferred_dividend_tax"] = str(dec(state.get("deferred_dividend_tax", "0")) - tax)
        state["cash_lots"].append({"lot_id": "dividend:" + corporate_id, "amount": str(gross - tax), "available_at": at.isoformat(),
                                   "reusable_on": at.date().isoformat(), "withdrawable_on": at.date().isoformat()})
        receivable.update(amount="0", nav_amount="0")
        item.update(distributed=True, status="paid", deferred_tax="0")
        return self.account._commit(action_id, request, state, current["sequence"], at=at, visible_at=at,
            entries=[{"account": "cash", "amount": str(gross - tax)}, {"account": "receivables", "amount": str(-gross)},
                     {"account": "payables", "amount": str(-tax)}])
