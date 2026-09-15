from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.budget import CostBudget, CostUnavailable, PriceTable
from advisor.research.experiments.clock import PhaseClock
from advisor.research.experiments.compute import CandidateComputeCosts, CPU_SEMANTICS
from advisor.research.experiments.costs import effective_budget
from advisor.research.experiments.gateway import CandidateGateway
from advisor.research.experiments.guardian import _publish
from advisor.research.experiments.orders import StagePlans
from advisor.research.experiments.phase_process import PhaseCandidateProcesses, CandidateProcessRecovery
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_budget import budget, registry
from tests.advisor.research.test_experiment_clock import empty
from tests.advisor.research.test_experiment_contracts import SESSIONS
from tests.advisor.research.test_experiment_guardian import guarded
from tests.advisor.research.test_experiment_isolation import runtime, limits
from tests.advisor.research.test_experiment_phase_process import PRELUDE, channel_limits


@pytest.fixture
def compute(budget, runtime, tmp_path):
    api, table, envelope, real = budget
    table = deepcopy(table)
    table['tariffs'].append({'resource': 'candidate_cpu', 'kind': 'compute',
        'usage_semantics_hash': api.records.artifacts.put_json(CPU_SEMANTICS).content_hash,
        'meters': [{'meter_id': 'seconds', 'unit': 'cpu_second', 'usd_per_unit': '.5'}]})
    table = PriceTable.model_validate(table).model_dump(mode='json')
    envelope = {k: v for k, v in envelope.items() if k != 'replay_evidence_hash'}
    envelope['price_table_hash'] = digest(table)
    envelope['replay_evidence_hash'] = api.records.artifacts.put_json(envelope).content_hash
    api.initialize(table, envelope, action_id='initialize')
    clock = PhaseClock(api.records, api.lease)
    clock.create(action_id='clock')
    clock.activate(empty(), action_id='active')
    account = SimulatedAccount(api.records, api.lease, calendar_sessions=SESSIONS, calendar_hash=api.sealed.tasks[0].calendar_hash)
    account.initialize(action_id='account-initialize')
    plans = StagePlans(account, clock, main_actor_id='main')
    client = CandidateGateway(clock.session('main'), clock, account, plans, products=(), plan_context=lambda *_: ({}, []))
    runner = guarded(api.records.artifacts, runtime, tmp_path / 'guardians')
    meter = CandidateComputeCosts(api, resource='candidate_cpu', fixture_maximum_cpu_seconds='2')
    return api, client, runner, meter, real


def identity(client, name='process'):
    session = client.session
    return digest([session.scope.test_id, session.scope.phase_id, session.actor_id, session.is_child, name])


def execute(fixture, limits, name='process', meter=None):
    _, client, runner, original, _ = fixture
    return PhaseCandidateProcesses(client, runner, compute=meter or original).run(name, limits=limits, channel_limits=channel_limits())


def invocation(fixture, name='process'):
    api, client, _, _, _ = fixture
    return effective_budget(api.records, api.lease.test_id)['value']['invocations']['candidate-compute:' + identity(client, name)]


@pytest.mark.parametrize('registry', [PRELUDE + "assert call('observe',{},'observe')['status']=='available'\nsum(range(200000))\n"], indirect=True)
def test_real_cpu_is_charged_from_guardian_and_retries_do_not_double_count(compute, limits):
    api, client, runner, meter, _ = compute
    result = execute(compute, limits)
    billed = invocation(compute)
    seconds = Decimal(str(result['user_cpu_seconds'])) + Decimal(str(result['system_cpu_seconds']))
    assert seconds > 0 and billed['receipt']['units'] == {'seconds': str(seconds)}
    assert Decimal(billed['cost_usd']) == seconds * Decimal('.5')
    assert billed['receipt']['supplier_bill_usd'] is None
    assert api.read()['buckets']['research']['held'] == '0'
    assert api.read()['buckets']['environment']['remaining'] == '2'
    assert api.read()['buckets']['evaluation']['remaining'] == '1'
    before = api.read()
    assert meter.collect(identity(client), runner)['status'] == 'settled'
    assert api.read() == before and not result['compute_result']['resource_scope_complete']


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_missing_physical_capability_and_insufficient_budget_never_dispatch(compute, limits):
    api, client, runner, _, _ = compute
    meter = CandidateComputeCosts(api, resource='candidate_cpu')
    with pytest.raises(CostUnavailable, match='capability_missing'):
        execute(compute, limits, meter=meter)
    assert not api._state()['value']['invocations']
    meter = CandidateComputeCosts(api, resource='candidate_cpu', fixture_maximum_cpu_seconds='15')
    with pytest.raises(CostUnavailable, match='insufficient_call_budget'):
        execute(compute, limits, name='too-large', meter=meter)
    assert api.records.projection(api.lease.test_id, 'candidate_processes') is None
    assert not list(runner.evidence_root.glob('*/launch.json'))


@pytest.mark.parametrize('registry', ['sum(range(100000))'], indirect=True)
def test_actual_overrun_is_charged_and_stops_new_research(compute, limits):
    api, _, _, _, _ = compute
    meter = CandidateComputeCosts(api, resource='candidate_cpu', fixture_maximum_cpu_seconds='.000001')
    execute(compute, limits, meter=meter)
    assert Decimal(invocation(compute)['cost_usd']) > Decimal('.0000005')
    assert api.read()['failures'][0]['code'] == 'cost_upper_bound_exceeded'
    with pytest.raises(CostUnavailable, match='cost_accounting_invalid'):
        execute(compute, limits, name='next', meter=meter)


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_reservation_before_process_claim_can_be_recovered_without_dispatch(compute, limits, monkeypatch):
    api, client, runner, meter, _ = compute
    original = api.start
    def interrupted(*_, **__):
        raise KeyboardInterrupt('before atomic start')
    monkeypatch.setattr(api, 'start', interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute(compute, limits)
    monkeypatch.setattr(api, 'start', original)
    assert invocation(compute)['status'] == 'reserved'
    assert api.records.projection(api.lease.test_id, 'candidate_processes') is None
    assert meter.collect(identity(client), runner)['status'] == 'released_unstarted'
    assert api.read()['buckets']['research']['held'] == '0'
    with pytest.raises(Conflict):
        execute(compute, limits)
    assert not list(runner.evidence_root.glob('*/child-intent.json'))


def interrupted_after_start(compute, limits):
    api = compute[0]
    count = [0]
    def fail(point):
        if point == 'after_commit':
            count[0] += 1
            if count[0] == 2:
                raise KeyboardInterrupt('atomic start response lost')
    api.records.fault = fail
    with pytest.raises(KeyboardInterrupt):
        execute(compute, limits)
    api.records.fault = lambda _: None


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_started_but_proven_not_dispatched_settles_zero_candidate_cpu(compute, limits):
    api, client, runner, meter, _ = compute
    interrupted_after_start(compute, limits)
    assert invocation(compute)['status'] == 'started'
    process = api.records.projection(api.lease.test_id, 'candidate_processes')['value']['calls'][identity(client)]
    assert process['compute']['invocation_id'] == invocation(compute)['bound']['invocation_id']
    result = CandidateProcessRecovery(api.records, api.lease, runner, compute=meter).reconcile(identity(client))
    assert result['status'] == 'not_started'
    assert invocation(compute)['status'] == 'settled'
    assert Decimal(invocation(compute)['cost_usd']) == 0
    assert invocation(compute)['receipt']['supplier_bill_usd'] is None


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_unknown_guardian_usage_keeps_original_hold(compute, limits):
    api, client, runner, meter, _ = compute
    interrupted_after_start(compute, limits)
    process = api.records.projection(api.lease.test_id, 'candidate_processes')['value']['calls'][identity(client)]
    directory, request = runner._load(process['guardian'])
    _publish(directory, 'child-intent.json', {'request_hash': digest(request)})
    assert meter.collect(identity(client), runner)['status'] == 'unsettled'
    assert api.read()['buckets']['research']['held'] == '1.0'
    assert invocation(compute)['receipt'] is None


def run_without_settling(compute, limits, monkeypatch):
    meter = compute[3]
    original = meter.collect
    def failed(*_, **__):
        raise KeyboardInterrupt('usage delivery interrupted')
    monkeypatch.setattr(meter, 'collect', failed)
    with pytest.raises(KeyboardInterrupt):
        execute(compute, limits)
    monkeypatch.setattr(meter, 'collect', original)


@pytest.mark.parametrize('registry', ['sum(range(100000))'], indirect=True)
@pytest.mark.parametrize('interrupt', [False, True])
def test_terminal_cost_overlay_uses_same_real_receipt_without_reopening_test(compute, limits, monkeypatch, interrupt):
    api, client, runner, meter, _ = compute
    run_without_settling(compute, limits, monkeypatch)
    api.records.transition(api.lease.test_id, 'cancelled', action_id='cancel-test', lease=api.lease)
    before = api.records.projection(api.lease.test_id, 'cost_budget')
    if interrupt:
        def failed(point):
            if point == 'after_record':
                raise KeyboardInterrupt('terminal measurement interrupted')
        api.records.fault = failed
        with pytest.raises(KeyboardInterrupt):
            meter.collect(identity(client), runner)
        api.records.fault = lambda _: None
        assert invocation(compute)['status'] == 'started'
    assert meter.collect(identity(client), runner)['status'] == 'settled'
    assert Decimal(invocation(compute)['cost_usd']) > 0
    assert api.records.projection(api.lease.test_id, 'cost_budget') == before
    assert api.records.status(api.lease.test_id) == 'cancelled'
    assert meter.collect(identity(client), runner)['status'] == 'settled'


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_main_and_same_named_child_have_separate_shared_budget_charges(compute, limits):
    api, client, runner, meter, _ = compute
    execute(compute, limits)
    child = client.child('main')
    PhaseCandidateProcesses(child, runner, compute=meter).run('process', limits=limits, channel_limits=channel_limits())
    calls = api._state()['value']['invocations']
    assert len(calls) == 2
    assert 'candidate-compute:' + identity(client) in calls
    assert 'candidate-compute:' + identity(child) in calls
    assert Decimal(api.read()['buckets']['research']['settled']) == sum(Decimal(call['cost_usd']) for call in calls.values())


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
def test_new_lease_collects_usage_even_if_process_cleanup_was_already_recorded(compute, limits, monkeypatch):
    api, client, runner, meter, real = compute
    run_without_settling(compute, limits, monkeypatch)
    real[0] += timedelta(seconds=10001)
    lease = api.records.claim(api.lease.test_id, worker_id='replacement', lease_seconds=10000)
    new_meter = CandidateComputeCosts(CostBudget(api.records, lease), resource='candidate_cpu')
    with pytest.raises(Fenced):
        meter.collect(identity(client), runner)
    CandidateProcessRecovery(api.records, lease, runner, compute=new_meter).reconcile(identity(client))
    assert invocation(compute)['status'] == 'settled'


@pytest.mark.parametrize('registry', ['pass'], indirect=True)
@pytest.mark.parametrize('point', ['before_event', 'after_commit'])
def test_measurement_and_charge_commit_together(compute, limits, monkeypatch, point):
    api, client, runner, meter, _ = compute
    run_without_settling(compute, limits, monkeypatch)
    def failed(actual):
        if actual == point:
            raise KeyboardInterrupt('meter transaction interrupted')
    api.records.fault = failed
    with pytest.raises(KeyboardInterrupt):
        meter.collect(identity(client), runner)
    api.records.fault = lambda _: None
    assert (invocation(compute)['status'] == 'settled') == (point == 'after_commit')
    assert meter.collect(identity(client), runner)['status'] == 'settled'
