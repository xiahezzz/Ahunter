"""Historical plan ingress times; no order creation or execution side effects."""
from datetime import datetime, timedelta

from .data.contracts import MarketRule
from .clock import PhaseDefinition
from zoneinfo import ZoneInfo


def submission_timing(phase, rule, *, order_latency_ms, receipt_latency_ms, operation):
    phase, rule = PhaseDefinition.model_validate(phase), MarketRule.model_validate(rule)
    if operation not in {"submit", "cancel"}:
        raise ValueError("unsupported ingress operation")
    if any(type(value) is not int or value < 0 for value in (order_latency_ms, receipt_latency_ms)):
        raise ValueError("ingress delays must be nonnegative milliseconds")
    if phase.plan_received_at is None or phase.trading_date is None:
        return {"status": "rejected", "code": "phase_does_not_accept_plans"}
    day, zone = phase.trading_date, ZoneInfo(phase.boundary.market_timezone)
    if not rule.effective_from <= day <= rule.effective_to:
        return {"status": "blocked", "code": "market_rule_not_effective"}
    auction_end = datetime.combine(day, rule.opening_auction.end, zone)
    if phase.kind == "postauction" and phase.boundary.event_cutoff != auction_end:
        return {"status": "blocked", "code": "phase_market_clock_conflict"}
    received = phase.plan_received_at
    if received.astimezone(zone).date() != day or received < phase.boundary.snapshot_at:
        return {"status": "blocked", "code": "plan_reception_window_invalid"}
    intervals = rule.accept_orders
    if any(left.end > right.start for left, right in zip(intervals, intervals[1:])):
        return {"status": "blocked", "code": "market_acceptance_intervals_invalid"}
    accepted = None
    for interval in intervals:
        start, end = (datetime.combine(day, t, zone) for t in (interval.start, interval.end))
        candidate = max(received, start) + timedelta(milliseconds=order_latency_ms)
        if candidate < end:
            accepted = candidate
            break
    if accepted is None:
        return {"status": "blocked", "code": "no_market_acceptance_window"}
    forbidden = operation == "cancel" and any(datetime.combine(day, span.start, zone) <= accepted < datetime.combine(day, span.end, zone)
                                               for span in rule.cancel_forbidden)
    session = "queued_for_matching"
    if datetime.combine(day, rule.opening_auction.start, zone) <= accepted < datetime.combine(day, rule.opening_auction.end, zone):
        session = "opening_auction"
    elif any(datetime.combine(day, span.start, zone) <= accepted < datetime.combine(day, span.end, zone) for span in rule.continuous):
        session = "continuous"
    elif datetime.combine(day, rule.closing_auction.start, zone) <= accepted < datetime.combine(day, rule.closing_auction.end, zone):
        session = "closing_auction"
    return {"status": "rejected" if forbidden else "accepted", "code": "cancellation_forbidden" if forbidden else None,
            "platform_received_at": received.isoformat(), "exchange_received_at": accepted.isoformat(),
            "exchange_accepted_at": None if forbidden else accepted.isoformat(),
            "receipt_available_at": (accepted + timedelta(milliseconds=receipt_latency_ms)).isoformat(),
            "matching_session": None if forbidden else session,
            "can_participate_opening_auction": not forbidden and session == "opening_auction"}
