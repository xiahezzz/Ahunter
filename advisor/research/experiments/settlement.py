"""Explicit special-event consideration; no guessed recovery or terminal liquidation.

The host supplies source-bound, lot-specific conversion instructions. The complete
owned-lot snapshot must match before any retirement, receivable or replacement
share is committed. This is an internal executor, not an issuer-term interpreter.
"""
from datetime import datetime
from decimal import Decimal
from typing import Literal
import re
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .account import RESOURCE_HELD_ORDER_STATES, AccountUnavailable, ZERO, dec
from .contracts import Contract, Hash, Money, Name, NonnegativeInt, PositiveInt, numeric_field
from .corporate import assess_disposal
from .data.temporal import Availability, Boundary, aware
from .resolution import digest


class SettlementUnsupported(AccountUnavailable):
    pass


class LotConsideration(Contract):
    lot_id: Name
    old_quantity: PositiveInt = numeric_field("share")
    cash_amount: Money = numeric_field("CNY")
    replacement_quantity: NonnegativeInt = numeric_field("share")
    # Final allocation includes source-proven fractional treatment. The executor
    # never rounds a ratio to fabricate shares or cash-in-lieu.
    allocation_source_ref: Name
    replacement_cost_basis: Money | None = numeric_field("CNY_per_replacement_share", null="no replacement shares")


class SettlementTerms(Contract):
    settlement_id: Name
    kind: Literal["delisting", "merger", "cash_replacement"]
    security: Name
    effective_at: datetime
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    availability: Availability
    entitlement_snapshot_hash: Hash
    allocations: tuple[LotConsideration, ...] = Field(min_length=1)
    cash_payment_at: datetime | None
    cash_valuation: Literal["unconditional_fixed_claim"]
    replacement_security: Name | None
    replacement_tradable_at: datetime | None
    holding_start: Literal["inherit", "effective_date"]
    dividend_tax_treatment: Literal["dispose", "carry", "no_outstanding_obligations"]

    @model_validator(mode="after")
    def coherent(self):
        if not re.fullmatch(r"(?:SH|SZ)[0-9]{6}", self.security) or (self.replacement_security is not None and not re.fullmatch(r"(?:SH|SZ)[0-9]{6}", self.replacement_security)):
            raise ValueError("settlement requires canonical supported security identities")
        for entry in self.allocations:
            if entry.allocation_source_ref not in self.source_refs:
                raise ValueError("lot allocation source is absent from the source package")
            if bool(entry.replacement_quantity) != (entry.replacement_cost_basis is not None):
                raise ValueError("replacement shares need explicit source-bound cost basis")
        for instant in (self.effective_at, self.cash_payment_at, self.replacement_tradable_at):
            if instant is not None:
                aware(instant)
                if instant < self.effective_at:
                    raise ValueError("consideration cannot be usable before retirement")
        if len({entry.lot_id for entry in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate lot allocation")
        cash = sum((entry.cash_amount for entry in self.allocations), ZERO)
        shares = sum(entry.replacement_quantity for entry in self.allocations)
        if bool(cash) != (self.cash_payment_at is not None):
            raise ValueError("cash consideration needs exactly one explicit payment time")
        if bool(shares) != (self.replacement_security is not None and self.replacement_tradable_at is not None):
            raise ValueError("replacement shares need security and tradability terms")
        if not shares and (self.replacement_security is not None or self.replacement_tradable_at is not None):
            raise ValueError("unused replacement terms")
        if self.replacement_security == self.security:
            raise ValueError("special conversion must retire the old security identity")
        if any(entry.cash_amount != entry.cash_amount.quantize(Decimal("0.01")) for entry in self.allocations):
            raise ValueError("cash consideration must already have proven cent allocation")
        if self.dividend_tax_treatment == "carry" and (cash or self.holding_start != "inherit"):
            raise ValueError("tax-lot carry requires pure share conversion and inherited holding period")
        if self.dividend_tax_treatment == "dispose" and shares:
            raise ValueError("mixed/share tax disposal requires additional explicit allocation rules")
        return self


def entitlement_snapshot(state, security):
    """A source adapter must bind its allocations to these actual owned lots."""
    return {key: {"quantity": lot["quantity"], "acquired_on": lot["acquired_on"],
                  "cost_basis": lot["cost_basis"], "acquired_sequence": lot.get("acquired_sequence", 0)}
            for key, lot in state["stock_lots"].items() if lot["security"] == security and lot["quantity"]}


class SpecialSettlements:
    def __init__(self, account):
        self.account = account

    def apply(self, terms, *, at, action_id):
        terms = SettlementTerms.model_validate(terms)
        at = aware(at).astimezone(ZoneInfo(self.account.spec.clock.timezone))
        request = {"operation": "special_settlement", "terms": terms.model_dump(mode="json"), "at": at.isoformat()}
        prior = self.account._prior(action_id, request)
        if prior is not None:
            return prior
        boundary = Boundary(event_cutoff=at, snapshot_at=at, market_timezone=self.account.spec.clock.timezone)
        if at != terms.effective_at or not terms.availability.visible(boundary):
            raise SettlementUnsupported("settlement needs its proven effective time and available source terms")
        current = self.account._state()
        state = current["value"]
        if terms.settlement_id in state.get("special_settlements", {}):
            raise SettlementUnsupported("settlement identity has already retired its security")
        retired = {item["terms"]["security"] for item in state.get("special_settlements", {}).values()}
        if terms.security in retired or terms.replacement_security in retired:
            raise SettlementUnsupported("settlement cannot recreate a retired security identity")
        owned = entitlement_snapshot(state, terms.security)
        allocations = {entry.lot_id: entry for entry in terms.allocations}
        if (digest(owned) != terms.entitlement_snapshot_hash or owned.keys() != allocations.keys()
                or any(owned[key]["quantity"] != entry.old_quantity for key, entry in allocations.items())):
            raise SettlementUnsupported("settlement allocation does not match every owned lot")
        if any(order["security"] == terms.security and order["status"] in RESOURCE_HELD_ORDER_STATES
               for order in state["orders"].values()):
            raise SettlementUnsupported("outstanding orders must terminate before security retirement")
        # An issuer event cannot silently bypass an already registered share or
        # cash entitlement that should have been processed first.
        for item in state["corporate_actions"].values():
            action = item["terms"]
            if action["security"] == terms.security and not item["applied"]:
                raise SettlementUnsupported("registered corporate entitlements must be applied before retirement")
        gross_cash, tax, entries, created = ZERO, ZERO, [], []
        for key, allocation in allocations.items():
            lot = state["stock_lots"][key]
            obligations = [item for item in state.get("dividend_obligations", {}).values()
                           if item["lot_id"] == key and dec(item["basis_remaining"]) > 0]
            if terms.dividend_tax_treatment == "no_outstanding_obligations" and obligations:
                raise SettlementUnsupported("source tax treatment cannot discard outstanding dividend obligations")
            if terms.dividend_tax_treatment == "dispose":
                amount, tax_entries = assess_disposal(state, key, allocation.old_quantity, at)
                tax += amount
                entries.extend(tax_entries)
            replacement_id = "settlement:" + terms.settlement_id + ":" + key
            if terms.dividend_tax_treatment == "carry":
                if not allocation.replacement_quantity:
                    raise SettlementUnsupported("tax-lot carry cannot erase a retired lot without replacement")
                for obligation in obligations:
                    if obligation["shares_remaining"] != allocation.old_quantity:
                        raise SettlementUnsupported("tax-lot units need a complete source conversion allocation")
                    obligation.update(lot_id=replacement_id, shares_remaining=allocation.replacement_quantity)
            if allocation.replacement_quantity:
                old_basis = (dec(lot["cost_basis"]) * allocation.old_quantity).quantize(Decimal("0.01"))
                new_basis = (allocation.replacement_cost_basis * allocation.replacement_quantity).quantize(Decimal("0.01"))
                if new_basis > old_basis or (not allocation.cash_amount and new_basis != old_basis):
                    raise SettlementUnsupported("replacement cost allocation must preserve carried investment basis")
                state["stock_lots"][replacement_id] = {
                    "security": terms.replacement_security, "quantity": allocation.replacement_quantity,
                    "acquired_on": lot["acquired_on"] if terms.holding_start == "inherit" else at.date().isoformat(),
                    "acquired_sequence": lot.get("acquired_sequence", 0) if terms.holding_start == "inherit" else state.get("next_lot_sequence", 0),
                    "cost_basis": str(allocation.replacement_cost_basis),
                    "sellable_on": terms.replacement_tradable_at.astimezone(ZoneInfo(self.account.spec.clock.timezone)).date().isoformat(),
                    "available_at": terms.replacement_tradable_at.isoformat()}
                if terms.holding_start == "effective_date":
                    state["next_lot_sequence"] = state.get("next_lot_sequence", 0) + 1
                created.append(replacement_id)
                entries.append({"account": "shares", "security": terms.replacement_security, "quantity": allocation.replacement_quantity})
            lot["quantity"] = 0
            gross_cash += allocation.cash_amount
            entries.append({"account": "shares", "security": terms.security, "quantity": -allocation.old_quantity})
        if tax > gross_cash:
            raise SettlementUnsupported("fixed cash consideration cannot cover collectible dividend tax")
        net_cash = gross_cash - tax
        receivable_id = "settlement:" + terms.settlement_id
        if gross_cash:
            state["receivables"][receivable_id] = {"amount": str(net_cash), "nav_amount": str(net_cash), "qualified": True,
                "kind": "special_settlement", "source_ref": terms.source_refs[0], "gross_amount": str(gross_cash), "withheld_tax": str(tax)}
            entries.append({"account": "receivables", "amount": str(net_cash), "settlement_id": terms.settlement_id})
        state.setdefault("special_settlements", {})[terms.settlement_id] = {
            "terms": terms.model_dump(mode="json"), "status": "receivable" if gross_cash else "settled",
            "replacement_lot_ids": created, "receivable_id": receivable_id if gross_cash else None}
        return self.account._commit(action_id, request, state, current["sequence"], at=at, visible_at=at, entries=entries)

    def pay(self, settlement_id, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.account.spec.clock.timezone))
        request = {"operation": "special_settlement_pay", "settlement_id": settlement_id, "at": at.isoformat()}
        prior = self.account._prior(action_id, request)
        if prior is not None:
            return prior
        current = self.account._state()
        state = current["value"]
        item = state.get("special_settlements", {}).get(settlement_id)
        if item is None or item["status"] != "receivable":
            raise SettlementUnsupported("settlement has no unpaid fixed cash consideration")
        terms = SettlementTerms.model_validate(item["terms"])
        if at != terms.cash_payment_at:
            raise SettlementUnsupported("cash consideration requires its proven payment instant")
        receivable = state["receivables"][item["receivable_id"]]
        amount = dec(receivable["amount"])
        state["cash_lots"].append({"lot_id": item["receivable_id"], "amount": str(amount), "available_at": at.isoformat(),
                                  "reusable_on": at.date().isoformat(), "withdrawable_on": at.date().isoformat()})
        receivable.update(amount="0", nav_amount="0")
        item["status"] = "settled"
        return self.account._commit(action_id, request, state, current["sequence"], at=at, visible_at=at,
            entries=[{"account": "cash", "amount": str(amount)}, {"account": "receivables", "amount": str(-amount)}])
