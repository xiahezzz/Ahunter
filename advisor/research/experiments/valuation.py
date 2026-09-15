"""Source-bound raw-close NAV marks, separate from research costs and liquidation."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from .contracts import Contract, Name, PositiveDecimal
from .data.contracts import MarketRule
from .data.temporal import Availability, Boundary
from .registration import record_identity
from .resolution import digest
from .repository import Conflict
from .account import AccountUnavailable, dec, _cash


class RawClose(Contract):
    security: Name
    trade_date: date
    price: PositiveDecimal
    adjustment: Literal["raw"]
    status: Literal["active", "suspended", "delisting", "merger"]
    availability: Availability
    source_ref: Name
    corporate_actions_complete: bool
    corporate_action_ids: tuple[Name, ...]


class ValuationUnsupported(AccountUnavailable):
    pass


class AccountValuations:
    def __init__(self, account):
        self.account, self.records = account, account.records

    def _financial_state(self, cutoff):
        state = None
        sequence = None
        for row in self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='account' ORDER BY event_sequence", (self.account.lease.test_id,)):
            event = self.records.event(row[0])
            if datetime.fromisoformat(event["simulated_at"]) <= cutoff:
                state, sequence = event["value"]["updates"][0]["value"], event["sequence"]
        if state is None:
            raise ValuationUnsupported("account has no financial state at the valuation event cutoff")
        return state, sequence

    def mark(self, *, purpose, trade_date, snapshot_at, closes, market_rules, action_id):
        if purpose not in {"initial", "daily", "terminal"}:
            raise ValueError("unknown valuation purpose")
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("valuation snapshot must be timezone-aware")
        rules = tuple(MarketRule.model_validate(rule) for rule in market_rules)
        if not rules or any(not rule.effective_from <= trade_date <= rule.effective_to for rule in rules):
            raise ValuationUnsupported("valuation requires effective historical close rules")
        zone = ZoneInfo(self.account.spec.clock.timezone)
        if purpose == "initial":
            cutoff = self.account.task.initial_as_of
            if trade_date > cutoff.astimezone(zone).date():
                raise ValuationUnsupported("initial valuation cannot use future close prices")
        else:
            if trade_date not in self.account.task.trading_dates:
                raise ValuationUnsupported("valuation date is outside the declared task")
            if purpose == "terminal" and trade_date != self.account.task.trading_dates[-1]:
                raise ValuationUnsupported("terminal NAV must use the final declared trading day")
            cutoff = max(datetime.combine(trade_date, rule.closing_auction.end, zone) for rule in rules)
        if snapshot_at < cutoff:
            raise ValuationUnsupported("close evidence cannot precede the valuation cutoff")
        boundary = Boundary(event_cutoff=cutoff, snapshot_at=snapshot_at, market_timezone=self.account.spec.clock.timezone)
        sealed_through = self.account._state()["value"].get("sealed_through")
        if sealed_through is None or datetime.fromisoformat(sealed_through) < cutoff:
            raise ValuationUnsupported("account replay has not sealed the valuation cutoff")
        state, sequence = self._financial_state(cutoff)
        if datetime.fromisoformat(state["visible_at"]) > snapshot_at:
            raise ValuationUnsupported("required account receipts are later than the valuation snapshot")
        quotes = tuple(RawClose.model_validate(value) for value in closes)
        if len({value.security for value in quotes}) != len(quotes):
            raise ValuationUnsupported("ambiguous close prices")
        by_security = {value.security: value for value in quotes}
        quantities = {}
        for lot in state["stock_lots"].values():
            quantities[lot["security"]] = quantities.get(lot["security"], 0) + lot["quantity"]
        marks, market_value = [], Decimal("0")
        for security, quantity in sorted(quantities.items()):
            if not quantity:
                continue
            quote = by_security.get(security)
            if quote is None or not quote.availability.visible(boundary) or not quote.corporate_actions_complete:
                raise ValuationUnsupported("missing qualified raw close or corporate-action coverage")
            if quote.trade_date > trade_date or (quote.status == "active" and quote.trade_date != trade_date):
                raise ValuationUnsupported("close date does not match active security valuation")
            if quote.status in {"delisting", "merger"}:
                raise ValuationUnsupported("special security valuation needs an executable corporate-action rule")
            price, adjustments = _corporate_price(state, quote, cutoff)
            applicable = [rule for rule in rules if rule.exchange == security[:2]]
            if not applicable:
                raise ValuationUnsupported("security has no matching market close rule")
            if quote.availability.event_at.astimezone(zone).date() != quote.trade_date:
                raise ValuationUnsupported("price event date contradicts the close date")
            if not any(quote.availability.event_at.astimezone(zone).time().replace(tzinfo=None) == rule.closing_auction.end for rule in applicable):
                raise ValuationUnsupported("raw price is not an official session closing event")
            value = price * quantity
            market_value += value
            marks.append({"security": security, "quantity": quantity, "raw_price": str(quote.price), "market_value": str(value),
                          "valuation_price": str(price), "corporate_adjustments": adjustments,
                          "source_ref": quote.source_ref, "price_date": quote.trade_date.isoformat(),
                          "stale_valuation": quote.status == "suspended" and quote.trade_date < trade_date})
        receivables = Decimal("0")
        for item in state["receivables"].values():
            if item.get("qualified") is not True:
                raise ValuationUnsupported("unqualified receivable cannot be assigned a value")
            receivables += dec(item.get("nav_amount", item["amount"]))
        nav = _cash(state) + market_value + receivables - dec(state["payables"]) - dec(state["liabilities"])
        if purpose == "initial" and nav <= 0:
            raise ValuationUnsupported("initial NAV must be positive")
        value = {"purpose": purpose, "trade_date": trade_date.isoformat(), "event_cutoff": cutoff.isoformat(),
                 "snapshot_at": snapshot_at.isoformat(), "account_event_sequence": sequence,
                 "account_state_hash": digest(state), "cash": str(_cash(state)), "market_value": str(market_value),
                 "qualified_receivables": str(receivables), "payables": state["payables"], "liabilities": state["liabilities"],
                 "nav": str(nav), "marks": marks, "forced_liquidation": False, "research_cost_deducted": False,
                 "formal_ready": False, "input_hash": digest({"closes": quotes, "market_rules": rules})}
        artifact = self.records.artifacts.put_json(value)
        prepared = self.records.prepare(experiment_id=self.account.experiment_id, kind="valuation",
            record_id=record_identity(self.account.experiment_id, "valuation", [self.account.lease.test_id, action_id]),
            submission_identity=action_id, value=value, links=(("test", self.account.lease.test_id),), artifact_hashes=(artifact.content_hash,))
        with self.records._transaction():
            self.records._assert_lease(self.account.lease)
            return self.records._insert(prepared)

    def net_return(self, initial_id, terminal_id):
        start, end = self.records.read(initial_id), self.records.read(terminal_id)
        for record, purpose in ((start, "initial"), (end, "terminal")):
            if (record["kind"] != "valuation" or record["value"]["purpose"] != purpose or record["experiment_id"] != self.account.experiment_id
                    or self.records.related(record["record_id"]) != [{"relation": "test", "target_id": self.account.lease.test_id}]):
                raise Conflict("return requires initial/terminal NAV from the same Test")
        initial, terminal = dec(start["value"]["nav"]), dec(end["value"]["nav"])
        if initial <= 0:
            raise ValuationUnsupported("initial NAV must be positive")
        return {"initial_nav": str(initial), "terminal_nav": str(terminal), "net_return": str(terminal / initial - 1),
                "formal_ready": False, "formal_score_requires_episode_acceptance": True}


def _corporate_price(state, quote, cutoff):
    from .corporate import CorporateTerms
    known = {}
    for identity, item in state["corporate_actions"].items():
        terms = CorporateTerms.model_validate(item["terms"])
        if terms.security == quote.security:
            known[identity] = (item, terms)
    if set(quote.corporate_action_ids) - known.keys():
        raise ValuationUnsupported("corporate-action valuation lacks registered terms")
    due = {identity: pair for identity, pair in known.items() if pair[1].effective_at <= cutoff}
    if due.keys() - set(quote.corporate_action_ids):
        raise ValuationUnsupported("close corporate-action coverage omits a registered effective action")
    intervening = [terms.effective_at for _, terms in due.values() if terms.effective_at > quote.availability.event_at]
    if len(intervening) != len(set(intervening)):
        raise ValuationUnsupported("simultaneous corporate actions need an explicit composite price rule")
    price, adjustments = quote.price, []
    for identity, (item, terms) in sorted(due.items(), key=lambda entry: (entry[1][1].effective_at, entry[0])):
        if not item["applied"]:
            raise ValuationUnsupported("corporate action has not been applied at the valuation cutoff")
        if terms.effective_at <= quote.availability.event_at:
            continue
        action = terms.action
        before = price
        if action.kind == "dividend":
            price -= action.cash_per_share
        elif action.kind == "bonus":
            price /= 1 + action.shares_ratio
        elif action.kind == "split":
            price /= action.shares_ratio
        else:
            raise ValuationUnsupported("stale price needs executable corporate price-adjustment terms")
        if price <= 0:
            raise ValuationUnsupported("corporate adjustment produces a nonpositive valuation price")
        adjustments.append({"action_id": identity, "before": str(before), "after": str(price)})
    return price, adjustments
