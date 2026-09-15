from dataclasses import replace
from datetime import date
import json
import time

import pytest

from advisor.research.experiments.channel import ChannelLimits
from advisor.research.experiments.data.access import Product, ProductItem
from advisor.research.experiments.data.temporal import Availability
from advisor.research.experiments.isolation import DarwinCandidateRunner
from advisor.research.experiments.phase_process import PhaseCandidateProcesses
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from tests.advisor.research.test_experiment_gateway import gateway, frame
from tests.advisor.research.test_experiment_orders import setup, registry, bundle
from tests.advisor.research.test_experiment_isolation import runtime, limits
from tests.advisor.research.test_experiment_account import dt
from tests.advisor.research.test_experiment_execution import availability


PRELUDE = '''import json,sys
hello=json.loads(sys.stdin.readline())
assert hello['protocol']=='ahunter-candidate-tools@1'
def call(tool, arguments, action):
    print(json.dumps(dict(tool=tool,arguments=arguments,action_id=action)),flush=True)
    return json.loads(sys.stdin.readline())
'''


def controller(client, runtime):
    return PhaseCandidateProcesses(client, DarwinCandidateRunner(artifacts=client.records.artifacts, runtime=runtime))


def channel_limits():
    return ChannelLimits(frame_bytes=4096, response_bytes=16384, total_response_bytes=65536, max_requests=20)


@pytest.mark.parametrize("registry", [PRELUDE + '''
assert hello['role']=='main'
assert call('observe',{},'observe')['status']=='available'
assert call('save_memory',dict(summary='sealed memory',decisions=[],next_questions=[]),'save')['revision']==1
assert call('load_memory',{},'load')['memory']['summary']=='sealed memory'
assert call('submit_plan',dict(plan_id='empty-plan',instructions=[]),'plan')['status']=='accepted'
'''], indirect=True)
def test_real_process_uses_pinned_gateway_memory_and_plan(setup, runtime, limits):
    client = gateway(setup)
    host = controller(client, runtime)
    result = host.run("main-process", limits=limits, channel_limits=channel_limits())
    assert result["returncode"] == 0 and result["stop_reason"] == "exited", result
    assert result["quiescent"] and not result["formal_ready"]
    memory = client.records.projection(client.session.scope.test_id, "candidate_memory")
    assert memory["value"]["memory"]["summary"] == "sealed memory"
    assert "empty-plan" in client.plans._state()["value"]["plans"]
    with pytest.raises(Conflict, match="restart blindly"):
        host.run("main-process", limits=limits, channel_limits=channel_limits())
    client.clock.begin_close("completed", action_id="close")
    client.clock.finish_close(action_id="finish")


@pytest.mark.parametrize("registry", [PRELUDE + '''
assert hello['role']=='child'
assert call('observe',{},'observe')['status']=='available'
assert call('submit_plan',dict(plan_id='bad',instructions=[]),'plan')['code']=='tool_not_permitted'
assert call('save_memory',dict(summary='bad',decisions=[],next_questions=[]),'save')['code']=='tool_not_permitted'
'''], indirect=True)
def test_real_child_named_main_cannot_write(setup, runtime, limits):
    client = gateway(setup).child("main")
    result = controller(client, runtime).run("child", limits=limits, channel_limits=channel_limits())
    assert result["returncode"] == 0, result
    assert not client.plans._state()["value"]["plans"]
    assert client.records.projection(client.session.scope.test_id, "candidate_memory") is None


@pytest.mark.parametrize("registry", [PRELUDE + "call('submit_plan',dict(plan_id='late',instructions=[]),'plan')\n"], indirect=True)
def test_phase_close_blocks_response_and_waits_for_real_cleanup(setup, runtime, limits):
    checks = []
    def close(*_):
        setup[1].begin_close("completed", action_id="closing-during-call")
        with pytest.raises(Conflict, match="candidate processes"):
            setup[1].finish_close(action_id="too-soon")
        checks.append(True)
        return {}, []
    client = gateway(setup, resolver=close)
    result = controller(client, runtime).run("closing", limits=limits, channel_limits=channel_limits())
    assert checks and result["stop_reason"] == "phase_closed", result
    assert result["quiescent"] and not client.account._state()["value"]["orders"]
    setup[1].finish_close(action_id="after-reaped")


@pytest.mark.parametrize("registry", [PRELUDE + "call('query',dict(product='slow',start='2026-08-03',end='2026-08-03',limit=1),'query')\n"], indirect=True)
def test_slow_host_adapter_does_not_suspend_process_deadline(setup, runtime, limits):
    calls = []
    def slow(_):
        calls.append(True)
        time.sleep(0.5)
        return []
    client = gateway(setup, products=[Product("slow", "v1", False, slow)])
    result = controller(client, runtime).run("slow", limits=replace(limits, wall_seconds=0.3),
                                               channel_limits=channel_limits())
    assert calls and result["stop_reason"] == "wall_timeout", result
    assert result["wall_seconds"] < 0.5 and result["quiescent"]


@pytest.mark.parametrize("registry,reason", [
    ("print('not json',flush=True)\nimport time;time.sleep(1)", "invalid_frame"),
    ("print('{}\\n{}',flush=True)\nimport time;time.sleep(1)", "request_pipelining"),
    ("print('x'*5000,flush=True)\nimport time;time.sleep(1)", "frame_limit"),
    ("print('{',end='',flush=True)", "incomplete_frame"),
    ("print('{\"tool\":\"observe\",\"tool\":\"query\"}',flush=True)\nimport time;time.sleep(1)", "invalid_frame"),
], indirect=["registry"])
def test_bad_wire_input_stops_and_persists_hash_only(setup, runtime, limits, reason):
    client = gateway(setup)
    result = controller(client, runtime).run("malformed", limits=limits, channel_limits=channel_limits())
    assert result["stop_reason"] == reason and result["quiescent"], result
    assert "stdout" not in result and "stderr" not in result


@pytest.mark.parametrize("registry", [PRELUDE + "call('observe',{},'observe')\n"], indirect=True)
def test_response_limit_prevents_unbounded_input_to_candidate(setup, runtime, limits):
    client = gateway(setup)
    result = controller(client, runtime).run("large-response", limits=limits,
        channel_limits=replace(channel_limits(), response_bytes=16))
    assert result["stop_reason"] == "response_limit" and result["quiescent"]


@pytest.mark.parametrize("registry", ["pass"], indirect=True)
def test_closed_phase_never_creates_dispatch_claim(setup, runtime, limits):
    client = gateway(setup)
    client.clock.begin_close("completed", action_id="close")
    with pytest.raises(Fenced):
        controller(client, runtime).run("closed", limits=limits, channel_limits=channel_limits())
    assert client.records.projection(client.session.scope.test_id, "candidate_processes") is None


@pytest.mark.parametrize("registry", [PRELUDE + '''
result=call('query',dict(product='prices',security='SH600000',start='2026-08-03',end='2026-08-03',limit=10),'query')
assert [item['item_id'] for item in result['items']]==['visible']
assert 'future-secret' not in json.dumps(result)
'''], indirect=True)
def test_real_candidate_receives_only_time_filtered_product(setup, runtime, limits):
    def read(_):
        for name, at in (("visible", "2026-08-03T09:24:00"), ("future-secret", "2026-08-03T09:26:00")):
            yield ProductItem(name, "SH600000", date(2026, 8, 3),
                              Availability.model_validate(availability(dt(at))), {"fact": name})
    client = gateway(setup, products=[Product("prices", "fixture-v1", True, read)])
    result = controller(client, runtime).run("filtered", limits=limits, channel_limits=channel_limits())
    assert result["returncode"] == 0, result


@pytest.mark.parametrize("registry", [PRELUDE + "call('observe',{},'first')\ncall('observe',{},'second')\n"], indirect=True)
def test_request_count_is_enforced_across_exchanges(setup, runtime, limits):
    client = gateway(setup)
    result = controller(client, runtime).run("count", limits=limits,
        channel_limits=replace(channel_limits(), max_requests=1))
    assert result["stop_reason"] == "request_limit" and result["quiescent"]


@pytest.mark.parametrize("registry", ["import time;time.sleep(20)"], indirect=True)
def test_host_failure_reaps_before_recording_cleanup_and_reraising(setup, runtime, limits):
    client = gateway(setup)
    def broken():
        raise RuntimeError("host callback")
    with pytest.raises(RuntimeError, match="host callback"):
        controller(client, runtime).run("failure", limits=limits, channel_limits=channel_limits(), cancelled=broken)
    calls = client.records.projection(client.session.scope.test_id, "candidate_processes")["value"]["calls"]
    assert len(calls) == 1
    assert next(iter(calls.values()))["quiescent"]


@pytest.mark.parametrize("registry", ["pass"], indirect=True)
def test_uncertain_dispatch_commit_never_relaunches_or_claims_cleanup(setup, runtime, limits, monkeypatch):
    client = gateway(setup)
    host = controller(client, runtime)
    launches = []
    monkeypatch.setattr(host.runner, "run", lambda *a, **kw: launches.append(True))
    def fault(point):
        if point == "after_commit":
            raise KeyboardInterrupt("uncertain dispatch")
    client.records.fault = fault
    with pytest.raises(KeyboardInterrupt):
        host.run("uncertain", limits=limits, channel_limits=channel_limits())
    client.records.fault = lambda _: None
    with pytest.raises(Conflict, match="restart blindly"):
        host.run("uncertain", limits=limits, channel_limits=channel_limits())
    assert launches == []
    client.clock.begin_close("completed", action_id="closing")
    with pytest.raises(Conflict, match="candidate processes"):
        client.clock.finish_close(action_id="cannot-finish")


OVERFLOW_FRAME = frame("save_memory", {"summary": "must not commit", "decisions": [], "next_questions": []})


@pytest.mark.parametrize("registry", [PRELUDE + f"print({json.dumps(OVERFLOW_FRAME)!r},flush=True)\nsys.stdin.readline()\n"], indirect=True)
def test_output_overflow_cannot_dispatch_the_overflowing_request(setup, runtime, limits):
    client = gateway(setup)
    result = controller(client, runtime).run("overflow", limits=replace(limits, output_bytes=len(json.dumps(OVERFLOW_FRAME))),
                                               channel_limits=channel_limits())
    assert result["stop_reason"] == "output_limit"
    assert client.records.projection(client.session.scope.test_id, "candidate_memory") is None


@pytest.mark.parametrize("registry", ["pass"], indirect=True)
def test_closing_after_dispatch_claim_suppresses_known_unstarted_process(setup, runtime, limits, monkeypatch):
    client = gateway(setup)
    host = controller(client, runtime)
    launches = []
    monkeypatch.setattr(host.runner, "run", lambda *a, **kw: launches.append(True))
    def close(point):
        if point == "after_commit":
            client.records.fault = lambda _: None
            client.clock.begin_close("completed", action_id="close-before-process")
    client.records.fault = close
    result = host.run("suppressed", limits=limits, channel_limits=channel_limits())
    assert result["status"] == "not_started" and result["quiescent"]
    assert launches == []
    client.clock.finish_close(action_id="no-cleanup-needed")
