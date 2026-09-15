from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from advisor.db.migrate import migrate_database
from advisor.research.repository import ResearchRepository, ResearchRequestOwnershipLost
from advisor.research.service import ResearchService, ServiceExecutionResult
from advisor.research.work_queue import ExperimentWorkQueue, next_work, read_work
from advisor.web.api import create_app
from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.budget import CostBudget, PriceTable
from advisor.research.experiments.clock import phase_schedule
from advisor.research.experiments.compute import CandidateComputeCosts, CPU_SEMANTICS
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.data.access import Product
from advisor.research.experiments.episode import EpisodeReplay
from advisor.research.experiments.episode_candidates import EpisodeCandidates
from advisor.research.experiments.guardian import _publish
from advisor.research.experiments.records import ExperimentRecords, Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest, resolve
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input
from tests.advisor.research.test_experiment_bundles import bundle
from tests.advisor.research.test_experiment_contracts import calendar, SESSIONS
from tests.advisor.research.test_experiment_episode import program
from tests.advisor.research.test_experiment_episode_candidates import TRADE
from tests.advisor.research.test_experiment_execution import proof
from tests.advisor.research.test_experiment_guardian import guarded
from tests.advisor.research.test_experiment_isolation import runtime, limits
from tests.advisor.research.test_experiment_phase_process import PRELUDE, channel_limits
from tests.advisor.research.test_service import _submit


@pytest.fixture
def queued(registry, bundle, runtime, limits, tmp_path, request):
    service, experiment, _, package, real = registry
    raw = original_case()
    raw['budget'].update(mode='explicit', task_cost_limit='10', environment_reserve='2', evaluation_reserve='1')
    raw['runtime'].update(getattr(request, 'param', {}))
    sealed = resolve(raw, calendar=calendar).specification
    definition = service.definition(experiment, sealed, submission_identity='service-definition')
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, definition['record_id'], plan_input(definition, plan_id='service-plan'),
                             submission_identity='service-plan', purpose='tuning')
    records, test_id = service.records, plan['tests'][0]['record_id']
    records.transition(test_id, 'preflight', action_id='preflight')
    records.transition(test_id, 'queued', action_id='queued')
    rule, fees = deepcopy(bundle[4]['rules'][0]['payload']), bundle[4]['fees'][0]['payload']
    rule['continuous'][0]['end'] = '11:30:00'
    template = SimpleNamespace(account=SimpleNamespace(task=sealed.tasks[0]), lease=SimpleNamespace(test_id=test_id),
        clock=SimpleNamespace(sealed=sealed, schedule=phase_schedule(sealed, sealed.tasks[0].task_id)))
    recipe = program((template, (None, real, rule, fees)))
    semantics = records.artifacts.put_json({'fixture': True, 'scope': 'synthetic environment units'}).content_hash
    table = PriceTable.model_validate({'version': raw['budget']['price_table_ref'], 'currency': 'USD', 'basis': 'comparison_equivalent',
        'evidence_kind': 'fixture', 'source_hash': records.artifacts.put_json({'fixture': 'invented prices'}).content_hash,
        'tariffs': [{'resource': 'cpu', 'kind': 'compute', 'usage_semantics_hash': semantics,
                     'meters': [{'meter_id': 'seconds', 'unit': 'cpu_second', 'usd_per_unit': '.5'}]},
                    {'resource': 'candidate_cpu', 'kind': 'compute',
                     'usage_semantics_hash': records.artifacts.put_json(CPU_SEMANTICS).content_hash,
                     'meters': [{'meter_id': 'seconds', 'unit': 'cpu_second', 'usd_per_unit': '.5'}]}]}).model_dump(mode='json')
    envelope = {'specification_hash': sealed.specification_hash, 'task_id': sealed.tasks[0].task_id, 'price_table_hash': digest(table),
        'safety_factor': '2', 'no_model': True, 'full_market_replay': True, 'full_evaluation': True,
        'environment_units': {'cpu': {'seconds': '1'}}, 'evaluation_units': {'cpu': {'seconds': '1'}}}
    envelope['replay_evidence_hash'] = records.artifacts.put_json(envelope).content_hash
    descriptor = {'evidence_kind': 'fixture', 'program': recipe, 'table': table, 'envelope': envelope, 'rule': rule, 'fees': fees}
    queue = ExperimentWorkQueue(records)
    queue.enqueue_fixture(test_id, descriptor)
    runner = guarded(records.artifacts, runtime, tmp_path / 'guardian')
    builders = []
    products = []
    def factory_for(bound_records):
        def build(lease, supplied):
            builders.append(lease)
            budget = CostBudget(bound_records, lease)
            budget.initialize(supplied['table'], supplied['envelope'], action_id='budget-initialize')
            account = SimulatedAccount(bound_records, lease, calendar_sessions=SESSIONS, calendar_hash=sealed.tasks[0].calendar_hash)
            replay = EpisodeReplay(account, main_actor_id='main')
            replay.create(supplied['program'])
            meter = CandidateComputeCosts(budget, resource='candidate_cpu', fixture_maximum_cpu_seconds='2')
            def context(securities, phase):
                return ({s: {'market_rule': supplied['rule'], 'fee_schedule': supplied['fees']} for s in securities},
                        [proof((account, real, supplied['rule'], supplied['fees']), phase.plan_received_at, security=s) for s in sorted(securities)])
            return EpisodeCandidates(replay, runner, compute=meter, limits=limits, channel_limits=channel_limits(), products=products, plan_context=context)
        return build
    repository = ResearchRepository(records.db)
    executed = []
    def business(request, *_):
        executed.append(request.request_id)
        return ServiceExecutionResult(status='blocked', reason_code='quality_blocked')
    owner = ResearchService(repository, business, clock=records._now, experiment_queue=queue,
        experiment_runner_factory=factory_for(records), heartbeat_interval_seconds=.1)
    return SimpleNamespace(owner=owner, queue=queue, records=records, real=real, test_id=test_id, descriptor=descriptor,
        executed=executed, runner=runner, builders=builders, factory_for=factory_for, products=products, repository=repository)


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_one_queue_orders_business_and_real_five_day_episode_without_fabricated_business_request(queued):
    q = queued
    with q.repository.transaction():
        first = _submit(q.repository, identity='manual-first', at=q.real[0] - timedelta(seconds=1))
        last = _submit(q.repository, identity='scheduled-last', origin='scheduled', at=q.real[0] - timedelta(seconds=2))
    assert next_work(q.records.db).work_id == first.request_id
    assert q.owner.tick().status == 'blocked'
    work = q.owner.tick()
    assert work.work_id == q.test_id and work.state == 'waiting' and work.source_status == 'evaluating'
    assert q.records.status(q.test_id) == 'evaluating'
    account = q.records.projection(q.test_id, 'account')['value']
    assert account['orders']['buy']['filled_quantity'] == account['orders']['sell']['filled_quantity'] == 100
    assert q.owner.tick().request_id == last.request_id
    assert q.executed == [first.request_id, last.request_id]
    assert q.records.db.execute('SELECT COUNT(*) FROM research_requests').fetchone()[0] == 2
    assert q.owner.tick() is None
    q.queue.cancel(q.test_id)
    assert q.owner.tick().source_status == 'cancelled'
    with pytest.raises(Fenced):
        q.records.commit(q.builders[-1], phase_id='late', action_id='late', attempt=0, kind='late', payload={}, simulated_at=None)


def test_cancel_before_execution_never_builds_candidate_or_initializes_account(queued):
    q = queued
    original = q.queue.cancel(q.test_id)
    assert q.queue.cancel(q.test_id) == original
    result = q.owner.tick()
    assert result.source_status == 'cancelled' and result.state == 'finished'
    assert q.builders == [] and q.records.projection(q.test_id, 'account') is None
    assert q.queue.enqueue_fixture(q.test_id, q.descriptor) == result
    with pytest.raises(Conflict): q.queue.enqueue_fixture(q.test_id, {**q.descriptor, 'changed': True})


def test_service_epoch_fences_live_test_lease_before_successor_claim(queued):
    q = queued
    q.owner.lease_seconds = 1
    assert q.owner.start()
    old = q.queue.claim(q.test_id, service_owner_id=q.owner._lease_owner_id, claimed_by=q.owner._claim_owner_id, lease_seconds=100)
    q.records.transition(q.test_id, 'running', action_id='first-start', lease=old)
    peer = q.repository.open_peer()
    records = ExperimentRecords(peer.connection, artifacts=q.records.artifacts, clock=q.records.clock)
    queue = ExperimentWorkQueue(records)
    successor = ResearchService(peer, lambda *_: ServiceExecutionResult(status='blocked'), clock=records._now,
        experiment_queue=queue, experiment_runner_factory=q.factory_for(records))
    try:
        assert not successor.start()
        q.real[0] += timedelta(seconds=2)
        assert successor.start()
        assert q.records.status(q.test_id) == 'running' and read_work(q.records.db, q.test_id).state == 'queued'
        with pytest.raises(Fenced): q.records.renew(old, lease_seconds=100)
        new = queue.claim(q.test_id, service_owner_id=successor._lease_owner_id, claimed_by=successor._claim_owner_id, lease_seconds=100)
        assert new.generation == old.generation + 1
        with pytest.raises(Fenced): q.queue.claim(q.test_id, service_owner_id=q.owner._lease_owner_id, claimed_by=q.owner._claim_owner_id, lease_seconds=100)
        assert queue.claim(q.test_id, service_owner_id=successor._lease_owner_id, claimed_by=successor._claim_owner_id, lease_seconds=100) == new
    finally:
        peer.close()


def test_experiment_claim_cannot_jump_a_business_head_and_queue_claim_rolls_back_with_test_lease(queued):
    q = queued
    with q.repository.transaction():
        first = _submit(q.repository, identity='earlier', at=q.real[0] - timedelta(seconds=1))
    q.owner.start()
    args = dict(service_owner_id=q.owner._lease_owner_id, claimed_by=q.owner._claim_owner_id, lease_seconds=30)
    with pytest.raises(Fenced): q.queue.claim(q.test_id, **args)
    assert read_work(q.records.db, q.test_id).state == 'queued'
    q.owner.tick()
    def fault(point):
        if point == 'before_commit': raise RuntimeError('claim transaction lost')
    q.records.fault = fault
    with pytest.raises(RuntimeError): q.queue.claim(q.test_id, **args)
    q.records.fault = lambda _: None
    assert read_work(q.records.db, q.test_id).state == 'queued'
    assert q.records.db.execute('SELECT COUNT(*) FROM lagent_worker_leases WHERE test_id=?', (q.test_id,)).fetchone()[0] == 0
    assert q.queue.claim(q.test_id, **args).generation == 1


@pytest.mark.parametrize('registry', [PRELUDE + "call('observe',{},'observe')"], indirect=True)
def test_unknown_cleanup_holds_shared_lane_and_cancellation_does_not_fake_terminal(queued, monkeypatch):
    q = queued
    def uncertain(*args, process_handle, **kwargs):
        directory, request = q.runner._load(process_handle)
        _publish(directory, 'child-intent.json', {'request_hash': digest(request)})
        raise RuntimeError('unknown launch')
    monkeypatch.setattr(q.runner, 'run', uncertain)
    assert q.owner.tick().state == 'running'
    with q.repository.transaction():
        later = _submit(q.repository, identity='later-business', at=q.real[0] + timedelta(seconds=1))
    state = q.owner.status()
    assert state.active_request_id is None and state.active_test_id == q.test_id and state.queued_count == 1
    assert q.owner.tick().work_id == q.test_id
    q.queue.cancel(q.test_id)
    assert q.owner.tick().source_status == 'running'
    assert q.repository.get_request(later.request_id).status == 'queued'
    calls = q.records.projection(q.test_id, 'candidate_processes')['value']['calls']
    assert len(calls) == 1 and not next(iter(calls.values()))['quiescent']


@pytest.mark.parametrize('queued', [{'lease_seconds': 2, 'heartbeat_seconds': 1}], indirect=True)
@pytest.mark.parametrize('registry', [PRELUDE + "call('query',dict(product='slow',start='2026-08-02',end='2026-08-02',limit=1),'query')"], indirect=True)
def test_service_peer_renews_test_while_real_candidate_waits_on_host_adapter(queued):
    q = queued
    q.records.clock = lambda: datetime.now(timezone.utc)
    q.owner.lease_seconds = 2
    observed = []
    def slow(_):
        before = datetime.now(timezone.utc)
        time.sleep(2.5)
        expires = q.records.db.execute('SELECT expires_at FROM lagent_worker_leases WHERE test_id=?', (q.test_id,)).fetchone()[0]
        observed.append(datetime.fromisoformat(expires) > before + timedelta(seconds=2.5))
        q.owner.stop()
        return []
    q.products.append(Product('slow', 'fixture-v1', False, slow))
    result = q.owner.tick()
    assert observed == [True]
    assert result.source_status == 'cancelled'
    calls = q.records.projection(q.test_id, 'candidate_processes')['value']['calls']
    assert len(calls) == 1 and next(iter(calls.values()))['quiescent']
    budget = q.records.projection(q.test_id, 'cost_budget')['value']
    assert all(value['status'] == 'settled' for value in budget['invocations'].values())


def test_schema_backfills_legacy_requests_and_repeated_migration_keeps_active_claim(queued):
    q = queued
    with q.repository.transaction():
        request = _submit(q.repository, identity='legacy-queue', at=q.real[0] - timedelta(seconds=1))
        q.records.db.execute('DELETE FROM research_work_queue WHERE work_id=?', (request.request_id,))
    path = q.records.db.execute('PRAGMA database_list').fetchone()[2]
    migrate_database(Path(path))
    assert next_work(q.records.db).work_id == request.request_id
    q.owner.start()
    with q.repository.transaction():
        q.repository.claim_next_request(q.owner._claim_owner_id, claimed_at=q.real[0])
        q.records.db.execute('UPDATE research_work_queue SET service_owner_id=? WHERE work_id=?', (q.owner._lease_owner_id, request.request_id))
    before = read_work(q.records.db, request.request_id)
    migrate_database(Path(path))
    assert read_work(q.records.db, request.request_id) == before


def test_reentrant_service_tick_cannot_claim_second_work(queued):
    q = queued
    with q.repository.transaction():
        _submit(q.repository, identity='reentry', at=q.real[0] - timedelta(seconds=1))
    def reenter(*_):
        assert q.owner.tick() is None
        return ServiceExecutionResult(status='blocked', reason_code='quality_blocked')
    q.owner.executor = reenter
    assert q.owner.tick().status == 'blocked'
    assert read_work(q.records.db, q.test_id).state == 'queued'


def test_missing_queue_index_fences_worker_and_migration_restores_admission_and_cancel_fact(queued):
    q = queued
    q.owner.start()
    lease = q.queue.claim(q.test_id, service_owner_id=q.owner._lease_owner_id, claimed_by=q.owner._claim_owner_id, lease_seconds=100)
    q.records.transition(q.test_id, 'running', action_id='run', lease=lease)
    q.queue.cancel(q.test_id)
    with q.repository.transaction():
        q.records.db.execute('DELETE FROM research_work_queue WHERE work_id=?', (q.test_id,))
    with pytest.raises(Fenced, match='binding is missing'): q.records.renew(lease, lease_seconds=100)
    path = Path(q.records.db.execute('PRAGMA database_list').fetchone()[2])
    migrate_database(path)
    restored = read_work(q.records.db, q.test_id)
    assert restored.state == 'queued' and restored.source_status == 'running' and restored.cancel_requested
    with pytest.raises(Fenced): q.records.renew(lease, lease_seconds=100)


def test_business_write_is_fenced_when_shared_epoch_changes_before_request_recovery(queued):
    q = queued
    with q.repository.transaction():
        first = _submit(q.repository, identity='business-fence', at=q.real[0] - timedelta(seconds=1))
    q.owner.start()
    with q.repository.transaction():
        q.repository.claim_next_request(q.owner._claim_owner_id, claimed_at=q.real[0])
        q.records.db.execute('UPDATE research_work_queue SET service_owner_id=? WHERE work_id=?', (q.owner._lease_owner_id, first.request_id))
        q.repository.acquire_service_lease('successor', now=q.real[0] + timedelta(seconds=31), ttl_seconds=30)
    with pytest.raises(ResearchRequestOwnershipLost):
        q.repository.require_request_claim(first.request_id, q.owner._claim_owner_id)


def test_cancelled_legacy_queued_request_preserves_existing_eligibility_and_does_not_hide_test(queued):
    q = queued
    with q.repository.transaction():
        cancelled = _submit(q.repository, identity='legacy-cancelled', at=q.real[0] - timedelta(seconds=1))
        q.records.db.execute('UPDATE research_requests SET cancel_requested=1 WHERE request_id=?', (cancelled.request_id,))
    assert next_work(q.records.db).work_id == q.test_id


@pytest.mark.parametrize('registry', [TRADE], indirect=True)
def test_service_reconstruction_after_committed_fill_resumes_same_episode_without_double_trade(queued):
    q = queued
    q.owner.lease_seconds = 1
    original = q.owner.experiment_runner_factory
    def crash_factory(lease, descriptor):
        controller = original(lease, descriptor)
        def crash(point):
            pending = controller.replay._state()['value']['pending']
            if point == 'after_effect' and pending and pending['command']['kind'] == 'minute':
                raise SystemExit('service lost after fill before cursor')
        controller.replay.fault = crash
        return controller
    q.owner.experiment_runner_factory = crash_factory
    with pytest.raises(SystemExit): q.owner.tick()
    prior_lease = q.builders[-1]
    assert q.records.projection(q.test_id, 'account')['value']['orders']['buy']['filled_quantity'] == 100
    assert q.records.projection(q.test_id, 'episode')['value']['pending']['command']['kind'] == 'minute'
    q.real[0] += timedelta(seconds=2)
    peer = q.repository.open_peer()
    records = ExperimentRecords(peer.connection, artifacts=q.records.artifacts, clock=q.records.clock)
    successor = ResearchService(peer, lambda *_: ServiceExecutionResult(status='blocked'), clock=records._now,
        experiment_queue=ExperimentWorkQueue(records), experiment_runner_factory=q.factory_for(records))
    try:
        result = successor.tick()
        assert result.state == 'waiting' and result.source_status == 'evaluating'
        orders = records.projection(q.test_id, 'account')['value']['orders']
        assert orders['buy']['filled_quantity'] == orders['sell']['filled_quantity'] == 100
        assert records.projection(q.test_id, 'account')['value']['fees_assessed']['commission'] == '10.00'
        assert q.builders[-1].generation == prior_lease.generation + 1
        with pytest.raises(Fenced): q.records.renew(prior_lease, lease_seconds=100)
    finally:
        peer.close()


def test_existing_status_api_reports_shared_test_without_inserting_a_business_request(queued, tmp_path):
    q = queued
    q.records.clock = lambda: datetime.now(timezone.utc)
    q.owner.start()
    lease = q.queue.claim(q.test_id, service_owner_id=q.owner._lease_owner_id, claimed_by=q.owner._claim_owner_id, lease_seconds=30)
    q.records.transition(q.test_id, 'running', action_id='run', lease=lease)
    path = Path(q.records.db.execute('PRAGMA database_list').fetchone()[2])
    with TestClient(create_app(state_dir=tmp_path / 'state', db_path=path)) as client:
        response = client.get('/api/research/requests/current')
    assert response.status_code == 200
    value = response.json()
    assert value['service']['state'] == 'running' and value['service']['active_test_id'] == q.test_id
    assert value['service']['active_request_id'] is None and value['requests'] == []
