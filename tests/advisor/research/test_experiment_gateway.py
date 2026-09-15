from datetime import date
import json

import pytest

from advisor.research.experiments.gateway import CandidateGateway
from advisor.research.experiments.data.access import Product, ProductItem
from advisor.research.experiments.data.temporal import Availability
from tests.advisor.research.test_experiment_orders import setup, registry, bundle, postauction
from tests.advisor.research.test_experiment_account import dt
from tests.advisor.research.test_experiment_execution import proof, availability
from tests.advisor.research.test_experiment_clock import finish, empty


def gateway(setup, *, resolver=None, products=()):
    fixture, session, plans = postauction(setup)
    def context(securities, phase):
        return ({security: {"market_rule": fixture[2], "fee_schedule": fixture[3]} for security in securities},
                [proof(fixture, phase.plan_received_at, security=security) for security in sorted(securities)])
    return CandidateGateway(session, setup[1], fixture[0], plans, products=products, plan_context=resolver or context)


def frame(tool, arguments=None, action_id="call"):
    return {"action_id": action_id, "tool": tool, "arguments": arguments or {}}


def intent(*, plan_id="candidate-plan"):
    return {"plan_id": plan_id, "instructions": [{"kind": "new", "order": {
        "order_id": "candidate-buy", "security": "SH600000", "side": "buy", "quantity": 100, "limit_price": "10"}}]}


def test_trade_intent_is_host_enriched_and_response_contains_no_ledger_or_future_effects(setup):
    client = gateway(setup)
    response = client.call_json(json.dumps(frame("submit_plan", intent())))
    assert response == {"status": "accepted", "plan_id": "candidate-plan"}
    stored = client.account._state()["value"]["orders"]["candidate-buy"]
    assert stored["fee_schedule"] == setup[0][3] and stored["market_rule"] == setup[0][2]
    assert stored["status"] == "pending_acceptance"
    assert client.call(frame("submit_plan", intent())) == response
    assert client.call(frame("submit_plan", intent(), action_id="transport-retry")) == response
    assert len(client.plans._state()["value"]["plans"]) == 1


@pytest.mark.parametrize("field,value", [("fee_schedule", {}), ("market_rule", {}), ("at", "2030-01-01"), ("commission_rate", "0")])
def test_candidate_cannot_supply_host_rule_fee_or_time_fields(setup, field, value):
    client = gateway(setup)
    request = intent()
    request["instructions"][0]["order"][field] = value
    before = client.account._state()
    assert client.call(frame("submit_plan", request))["code"] == "invalid_arguments"
    assert client.account._state() == before


@pytest.mark.parametrize("tool", ["native_search", "open_file", "read_artifact", "evaluate", "reset", "__dict__", "delegate"])
def test_unavailable_or_forbidden_tools_are_denied_without_exposing_host_objects(setup, tool):
    client = gateway(setup)
    response = client.call(frame(tool, {"path": "/private/host", "test_id": "another-test"}))
    assert response == {"status": "error", "code": "invalid_tool_request"}
    event = client.records.db.execute("SELECT value_json FROM lagent_events WHERE kind='candidate_tool' ORDER BY event_sequence DESC LIMIT 1").fetchone()[0]
    assert "/private/host" not in event and "another-test" not in event


def test_observe_uses_frozen_boundary_and_never_returns_current_execution_projection(setup):
    client = gateway(setup)
    assert client.call(frame("submit_plan", intent()))["status"] == "accepted"
    response = client.call(frame("observe", action_id="observe"))
    assert response["account"]["cash"] == "200000" and response["account"]["frozen_cash"] == "0"
    assert response["phase"]["boundary"]["event_cutoff"] == dt("2026-08-03T09:25:00").isoformat()
    assert "orders" not in response["account"] and "execution" not in response and "package_hash" not in response
    assert client.call(frame("observe", {"as_of": "2030-01-01"}, action_id="future"))["code"] == "invalid_arguments"


def test_only_committed_same_test_memory_survives_new_phase_and_child_cannot_write_it(setup):
    client = gateway(setup)
    memory = {"summary": "public research summary", "decisions": ["wait for evidence"], "next_questions": ["check volume"]}
    child = client.child("main")
    assert child.call(frame("save_memory", memory))["code"] == "tool_not_permitted"
    assert child.call(frame("submit_plan", intent(), action_id="child-plan"))["code"] == "tool_not_permitted"
    assert client.call(frame("save_memory", memory)) == {"status": "committed", "revision": 1}
    assert client.call(frame("save_memory", memory)) == {"status": "committed", "revision": 1}
    assert child.call(frame("load_memory", action_id="child-memory"))["memory"] == memory
    finish(setup[1], "postauction")
    setup[1].activate(empty(), action_id="postmarket")
    resumed = CandidateGateway(setup[1].session("main"), setup[1], setup[0][0], setup[2], products=[], plan_context=client.plan_context)
    assert resumed.call(frame("load_memory"))["memory"] == memory
    assert resumed.call(frame("load_memory", {"test_id": "another-test"}, action_id="foreign"))["code"] == "invalid_arguments"


def test_memory_transaction_failure_leaves_last_committed_revision(setup):
    client = gateway(setup)
    first = {"summary": "committed", "decisions": [], "next_questions": []}
    assert client.call(frame("save_memory", first))["revision"] == 1
    def fault(point):
        if point == "after_projection": raise KeyboardInterrupt("fixture crash")
    client.records.fault = fault
    with pytest.raises(KeyboardInterrupt): client.call(frame("save_memory", {**first, "summary": "uncommitted"}, action_id="second"))
    client.records.fault = lambda _: None
    assert client.call(frame("load_memory", action_id="read"))["memory"] == first


def test_query_filters_future_rows_and_reuses_existing_audit_without_reinvoking_provider(setup):
    calls = []
    def read(request):
        calls.append(request)
        for identity, at in (("visible",dt("2026-08-03T09:24:00")),("future-secret",dt("2026-08-03T09:26:00"))):
            yield ProductItem(identity, "SH600000", date(2026,8,3), Availability.model_validate(availability(at)), {"fact": identity})
    client = gateway(setup, products=[Product("prices", "fixture-v1", True, read)])
    args = {"product": "prices", "security": "SH600000", "start": "2026-08-03", "end": "2026-08-03", "limit": 10}
    response = client.call(frame("query", args))
    assert [item["item_id"] for item in response["items"]] == ["visible"]
    assert "future-secret" not in json.dumps(response)
    assert client.call(frame("query", args)) == response and len(calls) == 1
    assert client.child("researcher").call(frame("data_catalog"))["products"] == [{"product": "prices", "version": "fixture-v1", "required": True}]


def test_provider_exception_text_and_host_context_secrets_never_reach_candidate(setup):
    def broken(*_): raise RuntimeError("secret-token /private/host future-price=999")
    client = gateway(setup, resolver=broken, products=[Product("broken", "v1", False, broken)])
    assert client.call(frame("submit_plan", intent())) == {"status": "error", "code": "platform_failure"}
    response = client.call(frame("query", {"product": "broken", "start": "2026-08-03", "end": "2026-08-03", "limit": 1}, action_id="query"))
    assert response["code"] == "product_quality_failure"
    assert "secret-token" not in json.dumps(response)


def test_phase_closure_during_host_context_resolution_blocks_plan_commit(setup):
    def resolver(securities, phase):
        setup[1].begin_close("completed", action_id="close-during-context")
        fixture = setup[0]
        return ({security: {"market_rule": fixture[2], "fee_schedule": fixture[3]} for security in securities}, [proof(fixture, phase.plan_received_at)])
    client = gateway(setup, resolver=resolver)
    assert client.call(frame("submit_plan", intent()))["code"] == "phase_closed"
    assert client.account._state()["value"]["orders"] == {}


def test_committed_plan_ack_recovers_after_gateway_response_crash_and_phase_close(setup):
    client = gateway(setup)
    count = [0]
    def fault(point):
        if point == "after_commit":
            count[0] += 1
            if count[0] == 1: raise KeyboardInterrupt("after plan before gateway response")
    client.records.fault = fault
    with pytest.raises(KeyboardInterrupt): client.call(frame("submit_plan", intent()))
    client.records.fault = lambda _: None
    setup[1].begin_close("completed", action_id="close")
    client.plan_context = lambda *_: (_ for _ in ()).throw(RuntimeError("must not re-resolve accepted plan"))
    assert client.call(frame("submit_plan", intent())) == {"status": "accepted", "plan_id": "candidate-plan"}
    assert client.call(frame("observe", action_id="closed"))["code"] == "phase_closed"


@pytest.mark.parametrize("payload", ['{"tool":"observe","arguments":NaN}', 'not json', '{"__proto__":{"path":"/private"}}'])
def test_wire_parser_returns_only_json_safe_fixed_errors(setup, payload):
    response = gateway(setup).call_json(payload)
    assert response == {"status": "error", "code": "invalid_tool_request"}


def test_denied_query_retry_keeps_result_and_action_identity_cannot_switch_tools(setup):
    client = gateway(setup)
    args = {"product": "ungranted", "start": "2026-08-03", "end": "2026-08-03", "limit": 1}
    response = client.call(frame("query", args))
    assert response["code"] == "product_not_permitted"
    assert client.call(frame("query", args)) == response
    assert client.call(frame("query", {**args, "limit": 2}))["code"] == "conflict"
    assert client.call(frame("observe"))["code"] == "conflict"
    assert client.call(frame("save_memory", {"summary": "overwrite", "decisions": [], "next_questions": []}))["code"] == "conflict"
    assert client.records.projection(client.account.lease.test_id, "candidate_memory") is None
    assert client.call(frame("observe", action_id="other"))["status"] == "available"
    assert client.call(frame("query", args, action_id="other"))["code"] == "conflict"


def test_memory_bound_to_other_candidate_is_neither_read_nor_overwritten(setup):
    from advisor.research.experiments.records import ProjectionUpdate
    client = gateway(setup)
    client.records.commit(client.account.lease, phase_id=client.session.scope.phase_id,
        action_id="foreign-memory-fixture", attempt=0, kind="fixture", payload={},
        simulated_at=client.session.scope.boundary.snapshot_at,
        updates=(ProjectionUpdate("candidate_memory", None, {"package_hash": "another-package",
            "revision": 7, "memory": {"summary": "hidden", "decisions": [], "next_questions": []}}),))
    before = client.records.projection(client.account.lease.test_id, "candidate_memory")
    assert client.call(frame("load_memory")) == {"status": "error", "code": "conflict"}
    assert client.call(frame("save_memory", {"summary": "replace", "decisions": [], "next_questions": []}, action_id="save"))["code"] == "conflict"
    assert client.records.projection(client.account.lease.test_id, "candidate_memory") == before
