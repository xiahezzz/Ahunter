from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.budget import CostBudget, PriceTable
from advisor.research.experiments.compute import CandidateComputeCosts, CPU_SEMANTICS
from advisor.research.experiments.episode import EpisodeReplay
from advisor.research.experiments.episode_candidates import EpisodeCandidates
from advisor.research.experiments.data.access import Product
from advisor.research.experiments.gateway import CandidateGateway
from advisor.research.experiments.guardian import _publish
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_budget import budget, registry
from tests.advisor.research.test_experiment_bundles import bundle
from tests.advisor.research.test_experiment_contracts import SESSIONS
from tests.advisor.research.test_experiment_episode import program
from tests.advisor.research.test_experiment_execution import proof
from tests.advisor.research.test_experiment_guardian import guarded
from tests.advisor.research.test_experiment_isolation import runtime, limits
from tests.advisor.research.test_experiment_phase_process import PRELUDE, channel_limits


TRADE = PRELUDE + '''
observed=call('observe',{},'observe')
assert observed['status']=='available'
phase=observed['phase']
day=phase['trading_date']
if phase['kind']=='postauction' and day in ('2026-08-03','2026-08-04'):
    side='buy' if day=='2026-08-03' else 'sell'
    intent=dict(plan_id=side,instructions=[dict(kind='new',order=dict(order_id=side,
        security='SH600000',side=side,quantity=100,limit_price='10' if side=='buy' else '9'))])
    assert call('submit_plan',intent,'plan')['status']=='accepted'
'''
RECOVER = TRADE + '''
key=phase['boundary']['snapshot_at']
memory=call('load_memory',{},'memory')['memory']
count=int(memory['summary'].split('|')[1])+1 if memory and memory['summary'].split('|')[0]==key else 1
assert call('save_memory',dict(summary=key+'|'+str(count),decisions=[],next_questions=[]),'save')['status']=='committed'
if count==1: sys.exit(1)
'''


@pytest.fixture
def host(budget, runtime, limits, bundle, tmp_path):
    api, table, envelope, real = budget
    table = deepcopy(table)
    table['tariffs'].append({'resource': 'candidate_cpu', 'kind': 'compute',
        'usage_semantics_hash': api.records.artifacts.put_json(CPU_SEMANTICS).content_hash,
        'meters': [{'meter_id': 'seconds', 'unit': 'cpu_second', 'usd_per_unit': '.5'}]})
    table = PriceTable.model_validate(table).model_dump(mode='json')
    envelope = {k: v for k, v in envelope.items() if k != 'replay_evidence_hash'}
    envelope['price_table_hash'] = digest(table)
    envelope['replay_evidence_hash'] = api.records.artifacts.put_json(envelope).content_hash
    api.initialize(table, envelope, action_id='initialize-budget')
    account = SimulatedAccount(api.records, api.lease, calendar_sessions=SESSIONS, calendar_hash=api.sealed.tasks[0].calendar_hash)
    replay = EpisodeReplay(account, main_actor_id='main')
    rule, fees = deepcopy(bundle[4]['rules'][0]['payload']), bundle[4]['fees'][0]['payload']
    rule['continuous'][0]['end'] = '11:30:00'
    fixture = account, real, rule, fees
    replay.create(program((replay, fixture)))
    def context(securities, phase):
        return ({security: {'market_rule': rule, 'fee_schedule': fees} for security in securities},
                [proof(fixture, phase.plan_received_at, security=security) for security in sorted(securities)])
    runner = guarded(api.records.artifacts, runtime, tmp_path / 'guardian')
    meter = CandidateComputeCosts(api, resource='candidate_cpu', fixture_maximum_cpu_seconds='2')
    controller = EpisodeCandidates(replay, runner, compute=meter, limits=limits, channel_limits=channel_limits(),
                                   products=(), plan_context=context)
    return controller, real


def run(controller, *, limit=500):
    for _ in range(limit):
        result = controller.step()
        if result['kind'] != 'progress':
            return result
    raise AssertionError('bounded fixture did not finish')


@pytest.mark.parametrize('registry', [TRADE, RECOVER], indirect=True)
def test_real_guardian_candidates_drive_five_day_trades_with_metered_phase_recovery(host, registry):
    controller, _ = host
    result = run(controller)
    assert result['kind'] == 'ready_for_evaluation'
    phases = controller._state()['value']['phases']
    assert len(phases) == 16
    # Recovery code commits twice per phase; successful code has no working memory.
    memory = controller.records.projection(controller.lease.test_id, 'candidate_memory')
    expected = 2 if memory else 1
    assert all(len(p['attempts']) == expected and p['outcome'] == 'completed' for p in phases.values())
    assert all(p['candidate_failures'] == expected - 1 for p in phases.values())
    calls = controller._calls()
    assert len(calls) == 16 * expected and all(c['quiescent'] and c['returncode'] in (0, 1) for c in calls.values())
    assert all('guardian_receipt_hash' in c for c in calls.values())
    costs = controller.compute.budget.read()
    assert Decimal(costs['buckets']['research']['settled']) > 0 and costs['buckets']['research']['held'] == '0'
    assert costs['buckets']['environment']['remaining'] == '2' and costs['buckets']['evaluation']['remaining'] == '1'
    orders = controller.replay.account._state()['value']['orders']
    assert orders['buy']['filled_quantity'] == orders['sell']['filled_quantity'] == 100
    assert len(controller.replay.plans._state()['value']['plans']) == 2
    if memory:
        assert memory['value']['revision'] == 32 and memory['value']['memory']['summary'].endswith('|2')
    before = controller._state()
    assert controller.step() == result and controller._state() == before


@pytest.mark.parametrize('budget', [{'runtime': {'candidate_recoveries': 0}}, {'runtime': {'candidate_recoveries': 2}}], indirect=True)
@pytest.mark.parametrize('registry', ['raise ValueError("candidate defect")'], indirect=True)
def test_recovery_allowance_resets_per_phase_and_exhaustion_keeps_next_scheduled_phase(host):
    controller, _ = host
    wanted = controller.clock.spec.runtime.candidate_recoveries + 1
    for _ in range(100):
        controller.step()
        phases = controller._state()['value']['phases']
        if len(phases) == 2 and list(phases.values())[1]['outcome']:
            break
    else:
        raise AssertionError('two research phases did not finish')
    assert all(len(p['attempts']) == wanted and p['candidate_failures'] == wanted
               and p['outcome'] == 'candidate_recoveries_exhausted' for p in phases.values())
    assert controller.replay._state()['value']['status'] == 'running'


def prepared(controller):
    for _ in range(20):
        controller.step()
        current = controller._state()
        if current and current['value']['phases']:
            phase = next(iter(current['value']['phases'].values()))
            if phase['attempts'][-1]['status'] == 'prepared':
                return phase['attempts'][-1]
    raise AssertionError('attempt not prepared')


def recover(controller, real):
    controller.records.renew(controller.lease, lease_seconds=1)
    real[0] += timedelta(seconds=2)
    lease = controller.records.claim(controller.lease.test_id, worker_id='recovered-owner', lease_seconds=10000)
    account = SimulatedAccount(controller.records, lease, calendar_sessions=SESSIONS, calendar_hash=controller.replay.account.calendar_hash)
    replay = EpisodeReplay(account, main_actor_id='main')
    meter = CandidateComputeCosts(CostBudget(controller.records, lease), resource='candidate_cpu', fixture_maximum_cpu_seconds='2')
    return EpisodeCandidates(replay, controller.runner, compute=meter, limits=controller.limits,
        channel_limits=controller.channel_limits, products=(), plan_context=controller.plan_context)


@pytest.mark.parametrize('boundary', ['after_dispatch_intent', 'after_process'])
@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_worker_loss_keeps_original_dispatch_and_does_not_charge_candidate_recovery(host, boundary):
    controller, real = host
    original = prepared(controller)
    def crash(point):
        if point == boundary: raise RuntimeError('worker lost')
    controller.fault = crash
    with pytest.raises(RuntimeError): controller.step()
    count = len(controller._calls())
    if boundary == 'after_dispatch_intent':
        assert controller.step()['kind'] == 'worker_recovery_required'
        assert count == 0
    new = recover(controller, real)
    with pytest.raises(Fenced): controller.step()
    assert run(new)['kind'] == 'ready_for_evaluation'
    phase = new._state()['value']['phases'][new.clock.schedule[0].phase_id]
    assert phase['candidate_failures'] == 0
    assert len(phase['attempts']) == (2 if count == 0 else 1)
    assert len(new._calls()) == 16
    if count:
        assert original['identity'] in new._calls()


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_insufficient_cpu_reservation_skips_all_research_and_still_replays_to_endpoint(host):
    controller, _ = host
    controller.compute.fixture_maximum = Decimal('20')
    assert run(controller)['kind'] == 'ready_for_evaluation'
    assert controller._calls() == {}
    phase = next(iter(controller._state()['value']['phases'].values()))
    assert phase['outcome'] == 'budget_exhausted'
    assert controller.replay.clock._state()['value']['research_stopped']
    assert controller.compute.budget.read()['buckets']['research']['settled'] == '0'


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_cancellation_after_dispatch_intent_fences_late_launch_and_does_not_need_a_new_lease(host):
    controller, _ = host
    prepared(controller)
    def crash(_): raise RuntimeError('paused before claim')
    controller.fault = crash
    with pytest.raises(RuntimeError): controller.step()
    controller.fault = lambda _: None
    controller.replay.request_stop('cancelled')
    assert run(controller)['kind'] == 'cancelled'
    assert controller._calls() == {}


@pytest.mark.parametrize('registry', [PRELUDE + "call('observe',{},'observe')"], indirect=True)
def test_recovery_cannot_change_pinned_candidate_limits(host):
    controller, _ = host
    prepared(controller)
    controller.limits = replace(controller.limits, output_bytes=controller.limits.output_bytes + 1)
    with pytest.raises(Conflict, match='configuration changed'): controller.step()


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_worker_loss_after_cpu_reservation_releases_stranded_hold_before_new_attempt(host):
    controller, real = host
    attempt = prepared(controller)
    original = controller.compute.prepare
    def reserve_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('reserved before process claim')
    controller.compute.prepare = reserve_then_crash
    with pytest.raises(RuntimeError): controller.step()
    assert controller._calls() == {}
    assert Decimal(controller.compute.budget.read()['buckets']['research']['held']) == 1
    new = recover(controller, real)
    assert run(new)['kind'] == 'ready_for_evaluation'
    invocation = new.compute.budget._state()['value']['invocations']['candidate-compute:' + attempt['identity']]
    assert invocation['status'] == 'released_unstarted'
    assert new.compute.budget.read()['buckets']['research']['held'] == '0'
    phase = new._state()['value']['phases'][new.clock.schedule[0].phase_id]
    assert phase['candidate_failures'] == 0 and len(phase['attempts']) == 2


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_unknown_guardian_cleanup_never_restarts_or_releases_cpu_hold(host, monkeypatch):
    controller, _ = host
    attempt = prepared(controller)
    def uncertain(*args, process_handle, **kwargs):
        directory, request = controller.runner._load(process_handle)
        _publish(directory, 'child-intent.json', {'request_hash': digest(request)})
        raise RuntimeError('lost launch acknowledgement')
    monkeypatch.setattr(controller.runner, 'run', uncertain)
    controller.step()
    for _ in range(3):
        assert controller.step()['kind'] == 'cleanup_required'
    assert list(controller._calls()) == [attempt['identity']]
    assert Decimal(controller.compute.budget.read()['buckets']['research']['held']) == 1
    controller.replay.request_stop('cancelled')
    assert controller.step()['kind'] == 'progress'  # Fence first, then wait for proof.
    assert controller.step()['kind'] == 'cleanup_required'
    assert controller.clock._state()['value']['status'] == 'closing'


@pytest.mark.parametrize('registry', [PRELUDE + "call('query',dict(product='source',start='2026-08-02',end='2026-08-02',limit=1),'query')"], indirect=True)
@pytest.mark.parametrize('required', [True, False])
def test_required_query_failure_blocks_even_if_candidate_exits_zero_but_optional_failure_does_not(host, required):
    controller, _ = host
    controller.products = (Product('source', 'fixture-v1', required, lambda _: []),)
    if required:
        assert run(controller)['kind'] == 'blocked'
        assert len(controller._calls()) == 1
    else:
        # Exercise the initial phase only; subsequent calendar snapshots are covered elsewhere.
        for _ in range(20):
            controller.step()
            phases = controller._state()['value']['phases']
            if phases and next(iter(phases.values()))['outcome']:
                break
        assert next(iter(phases.values()))['outcome'] == 'completed'
    audits = [controller.records.event(row[0])['value']['payload'] for row in controller.records.db.execute(
        "SELECT event_sequence FROM lagent_events WHERE test_id=? AND kind='candidate_tool'", (controller.lease.test_id,))]
    query = next(value for value in audits if value['tool'] == 'query')
    assert 'items' not in query['response'] and 'response_hash' in query['response']


@pytest.mark.parametrize('registry', [PRELUDE + "call('submit_plan',dict(plan_id='bad',instructions=[dict(kind='new',order=dict(order_id='bad',security='SH600000',side='sell',quantity=100,limit_price='10'))]),'plan')"], indirect=True)
def test_rejected_candidate_plan_is_not_a_platform_failure_or_recovery(host):
    controller, _ = host
    for _ in range(20):
        controller.step()
        phases = controller._state()['value']['phases']
        if phases and next(iter(phases.values()))['outcome']:
            break
    assert next(iter(phases.values()))['outcome'] == 'completed'
    assert next(iter(phases.values()))['candidate_failures'] == 0


@pytest.mark.parametrize('registry', [PRELUDE + "import time;time.sleep(5)"], indirect=True)
def test_cancel_during_real_candidate_waits_for_reap_and_cpu_settlement(host):
    controller, _ = host
    prepared(controller)
    assert controller.step(cancelled=lambda: any((controller.runner.evidence_root / identity / 'child-started.json').exists()
                                                 for identity in controller._calls()))['kind'] == 'progress'
    assert run(controller)['kind'] == 'cancelled'
    assert len(controller._calls()) == 1 and all(c['quiescent'] for c in controller._calls().values())
    assert controller.compute.budget.read()['buckets']['research']['held'] == '0'


@pytest.mark.parametrize('registry', [PRELUDE + "assert call('observe',{},'observe')['status']=='available'"], indirect=True)
def test_lost_final_wire_message_uses_verified_durable_receipt_without_rerunning(host, monkeypatch):
    controller, _ = host
    prepared(controller)
    original = controller.runner.run
    def lose_final(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('final wire acknowledgement lost')
    monkeypatch.setattr(controller.runner, 'run', lose_final)
    controller.step()
    assert len(controller._calls()) == 1
    call = next(iter(controller._calls().values()))
    assert call['quiescent'] and call['transport_code'] == 'process_monitor_failure'
    assert call['guardian_receipt_hash'] and call['returncode'] == 0
    controller.step()
    phase = next(iter(controller._state()['value']['phases'].values()))
    assert phase['outcome'] == 'completed' and phase['candidate_failures'] == 0


@pytest.mark.parametrize('registry', [PRELUDE + "call('observe',{},'observe')"], indirect=True)
def test_failed_tool_audit_cannot_disappear_as_a_successful_candidate(host, monkeypatch):
    controller, _ = host
    def audit_failure(*args, **kwargs):
        raise RuntimeError('cannot persist required tool audit')
    monkeypatch.setattr(CandidateGateway, '_record', audit_failure)
    assert run(controller)['kind'] == 'failed'
    call = next(iter(controller._calls().values()))
    assert call['gateway_failure'] == 'platform_failure' and call['quiescent']
    assert controller.compute.budget.read()['buckets']['research']['held'] == '0'


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_phase_timeout_before_dispatch_is_durable_and_does_not_spend_recovery_allowance(host):
    controller, real = host
    prepared(controller)
    timeout = controller.clock.spec.runtime.phase_wall_timeout_seconds
    controller.records.renew(controller.lease, lease_seconds=timeout + 10)
    real[0] += timedelta(seconds=timeout + 1)
    for _ in range(8):
        controller.step()
        if controller.clock._state()['value']['index'] == 1:
            break
    assert controller.clock._state()['value']['index'] == 1
    phase = next(iter(controller._state()['value']['phases'].values()))
    assert phase['outcome'] == 'phase_timeout' and phase['candidate_failures'] == 0
    assert controller._calls() == {}
