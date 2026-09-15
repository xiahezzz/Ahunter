from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from datetime import timedelta
import sqlite3
from threading import Barrier

import pytest

from advisor.research.experiments.budget import CostBudget, CostUnavailable, PriceTable
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.records import ExperimentRecords, Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest, resolve
from tests.advisor.research.test_experiment_contracts import calendar
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input


def proof(api, value):
    return api.records.artifacts.put_json(value).content_hash


@pytest.fixture
def budget(registry, request):
    service, experiment, _, package, clock = registry
    raw = original_case()
    raw['budget'].update(mode='explicit', task_cost_limit='10', environment_reserve='2', evaluation_reserve='1')
    overrides = dict(getattr(request, 'param', {}))
    raw['runtime'].update(overrides.pop('runtime', {}))
    raw['budget'].update(overrides)
    sealed = resolve(raw, calendar=calendar).specification
    definition = service.definition(experiment, sealed, submission_identity='budget-definition')
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, definition['record_id'], plan_input(definition, plan_id='budget-plan'),
                             submission_identity='budget-plan', purpose='tuning')
    records = service.records
    test_id = plan['tests'][0]['record_id']
    records.transition(test_id, 'preflight', action_id='preflight')
    records.transition(test_id, 'queued', action_id='queued')
    lease = records.claim(test_id, worker_id='budget-fixture', lease_seconds=10000)
    records.transition(test_id, 'running', action_id='running', lease=lease)
    api = CostBudget(records, lease)
    semantics = proof(api, {'fixture': True, 'counters': 'disjoint billable units; output includes reasoning'})
    table = {'version': raw['budget']['price_table_ref'], 'currency': 'USD', 'basis': 'comparison_equivalent',
        'evidence_kind': 'fixture', 'source_hash': proof(api, {'fixture': True, 'prices': 'invented for arithmetic tests'}),
        'tariffs': [{'resource': raw['model']['model'], 'kind': 'model', 'usage_semantics_hash': semantics,
            'meters': [{'meter_id': 'uncached_input', 'unit': 'token', 'usd_per_unit': '.1'},
                       {'meter_id': 'cached_input', 'unit': 'token', 'usd_per_unit': '.01'},
                       {'meter_id': 'billable_output', 'unit': 'token', 'usd_per_unit': '.2'}]},
                    {'resource': 'cpu', 'kind': 'compute', 'usage_semantics_hash': semantics,
                     'meters': [{'meter_id': 'seconds', 'unit': 'cpu_second', 'usd_per_unit': '.5'}]}]}
    table = PriceTable.model_validate(table).model_dump(mode='json')
    envelope = {'specification_hash': sealed.specification_hash, 'task_id': api.task_id, 'price_table_hash': digest(table),
        'safety_factor': '2', 'no_model': True, 'full_market_replay': True, 'full_evaluation': True,
        'environment_units': {'cpu': {'seconds': '1'}}, 'evaluation_units': {'cpu': {'seconds': '1'}}}
    envelope['replay_evidence_hash'] = proof(api, envelope)
    yield api, table, envelope, clock


def initialize(fixture):
    api, table, envelope, _ = fixture
    api.initialize(table, envelope, action_id='initialize')
    return api


def bound(api, identity='main-1', *, bucket='research', resource=None, maximum=None):
    value = api._state()['value']
    resource = resource or api.spec.model.model
    tariff = next(t for t in value['table']['tariffs'] if t['resource'] == resource)
    claim = {'invocation_id': identity, 'actor_id': identity, 'phase_id': 'phase-1', 'bucket': bucket,
        'resource': resource, 'adapter_ref': 'fixture-adapter@1', 'executor_ref': 'fixture-executor@1',
        'price_table_hash': value['price_table_hash'], 'usage_semantics_hash': tariff['usage_semantics_hash'],
        'maximum_units': maximum or {'uncached_input': '10', 'cached_input': '0', 'billable_output': '20'}}
    return {**claim, 'proof_hash': proof(api, claim)}


def receipt(api, claim, *, units=None, outcome='completed', bill=None):
    value = {k: claim[k] for k in ('resource','adapter_ref','executor_ref','price_table_hash','usage_semantics_hash')}
    value.update(units=units or {'uncached_input': '10', 'cached_input': '0', 'billable_output': '5'},
                 outcome=outcome, supplier_bill_usd=bill)
    return {**value, 'evidence_hash': proof(api, {'invocation_id': claim['invocation_id'], **value})}


def response(event):
    return event['value']['payload']['response']


def reserve(api, claim, action='reserve'):
    return api.reserve(claim, action_id=action, guard=lambda: None)


def start(api, identity='main-1', action='start'):
    return api.start(identity, action_id=action, guard=lambda: None)


def test_shared_budget_preserves_environment_and_has_no_accumulated_token_limit(budget):
    api = initialize(budget)
    assert api.spec.model.token_cap is None
    assert response(reserve(api, bound(api)))['maximum_cost_usd'] == '5.00'
    denied = response(reserve(api, bound(api, 'child-1'), 'child-reserve'))
    assert denied == {'status': 'denied', 'code': 'insufficient_call_budget', 'remaining': '2.00', 'required': '5.00'}
    env = bound(api, 'env', bucket='environment', resource='cpu', maximum={'seconds': '4'})
    assert response(reserve(api, env, 'env'))['status'] == 'reserved'
    assert api.read()['buckets']['evaluation']['remaining'] == '1'
    assert api.read()['formal_ready'] is False


def test_actual_disjoint_usage_releases_difference_and_invoice_is_separate(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api, claim)
    start(api)
    result = api.settle('main-1', receipt(api, claim), action_id='settle')
    assert response(result)['cost_usd'] == '2.00'
    assert api.read()['buckets']['research']['remaining'] == '5.00'
    assert api._state()['value']['invocations']['main-1']['receipt']['supplier_bill_usd'] is None
    assert api.read()['reconciled']
    assert response(reserve(api, bound(api, 'child-1'), 'child'))['status'] == 'reserved'


def test_retries_and_failed_attempts_are_charged_once_each(budget):
    api = initialize(budget)
    claim = bound(api)
    first = reserve(api, claim)
    assert reserve(api, claim, 'transport-retry') == first
    start(api)
    usage = receipt(api, claim, outcome='failed', bill='3')
    settled = api.settle('main-1', usage, action_id='settle')
    assert api.settle('main-1', usage, action_id='retry-settle') == settled
    with pytest.raises(Conflict): api.settle('main-1', receipt(api, claim, bill='4'), action_id='different')
    second = bound(api, 'retry-attempt-2')
    reserve(api, second, 'retry-reserve')
    start(api, 'retry-attempt-2', 'retry-start')
    api.settle('retry-attempt-2', receipt(api, second, outcome='cancelled'), action_id='retry-usage')
    assert api.read()['buckets']['research']['settled'] == '4.00'
    assert len(api._state()['value']['invocations']) == 2


def test_unknown_outcome_holds_maximum_until_late_usage_and_cannot_release_as_unstarted(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api, claim)
    start(api)
    api.mark_unknown('main-1', action_id='timeout')
    with pytest.raises(Conflict): api.release_unstarted('main-1', action_id='cancel-release')
    assert api.read()['buckets']['research']['held'] == '5.00'
    assert not api.read()['reconciled']
    assert response(reserve(api, bound(api, 'child'), 'child'))['status'] == 'denied'
    api.settle('main-1', receipt(api, claim, outcome='cancelled'), action_id='late-usage')
    assert api.read()['buckets']['research']['held'] == '0'
    assert api.read()['buckets']['research']['settled'] == '2.00'


def test_only_never_started_invocation_can_release_and_cannot_later_dispatch(budget):
    api = initialize(budget)
    reserve(api, bound(api))
    api.release_unstarted('main-1', action_id='release')
    assert api.read()['buckets']['research']['remaining'] == '7'
    with pytest.raises(Conflict): start(api)
    with pytest.raises(Conflict): api.settle('main-1', receipt(api, bound(api)), action_id='usage')


@pytest.mark.parametrize('bucket,resource,units,code', [
    ('research', None, {'uncached_input':'10','cached_input':'0','billable_output':'40'}, 'cost_upper_bound_exceeded'),
    ('environment', 'cpu', {'seconds':'8'}, 'platform_resource_failure')])
def test_real_overrun_is_charged_and_invalidates_further_research(budget,bucket,resource,units,code):
    api = initialize(budget)
    claim = bound(api, bucket=bucket, resource=resource, maximum={'seconds':'4'} if resource else None)
    reserve(api, claim)
    start(api)
    api.settle('main-1', receipt(api, claim, units=units), action_id='settle')
    assert api.read()['failures'] == [{'invocation_id':'main-1','code':code}]
    assert Decimal(api.read()['buckets'][bucket]['remaining']) < 0
    assert response(reserve(api, bound(api, 'child'), 'child'))['code'] == 'cost_accounting_invalid'
    env = bound(api, 'evaluation', bucket='evaluation', resource='cpu', maximum={'seconds':'1'})
    assert response(reserve(api, env, 'evaluation'))['status'] == 'reserved'


@pytest.mark.parametrize('mutation', ['proof', 'price', 'semantics', 'dimensions', 'environment-model'])
def test_missing_or_inconsistent_bound_cannot_authorize_call(budget, mutation):
    api = initialize(budget)
    claim = bound(api)
    if mutation == 'proof': claim['maximum_units']['billable_output'] = '1'
    if mutation == 'price': claim['price_table_hash'] = 'a'*64
    if mutation == 'semantics': claim['usage_semantics_hash'] = 'a'*64
    if mutation == 'dimensions':
        claim['maximum_units'].pop('cached_input')
        claim['proof_hash'] = proof(api, {k:v for k,v in claim.items() if k != 'proof_hash'})
    if mutation == 'environment-model': claim['bucket'] = 'environment'
    before = api._state()
    with pytest.raises((CostUnavailable, Conflict)): reserve(api, claim)
    assert api._state() == before


def test_bad_usage_does_not_turn_unknown_cost_into_zero(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api, claim)
    start(api)
    bad = receipt(api, claim, units={'uncached_input':'0','billable_output':'0'})
    with pytest.raises(CostUnavailable): api.settle('main-1', bad, action_id='bad')
    assert api.read()['buckets']['research']['held'] == '5.00'
    assert not api.read()['reconciled']


@pytest.mark.parametrize('point', ['after_record','after_event','after_projection','after_outbox','before_commit','after_commit'])
def test_budget_and_cost_evidence_are_atomic_and_replayable(budget, point):
    api = initialize(budget)
    claim = bound(api)
    def fault(actual):
        if actual == point: raise KeyboardInterrupt('fixture failure')
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): reserve(api, claim)
    api.records.fault = lambda _: None
    assert len(api._state()['value']['invocations']) == int(point == 'after_commit')
    reserve(api, claim)
    state = api._state()
    assert api.records.rebuild(api.lease)['cost_budget'] == state
    assert api._state() == state
    assert api.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='cost'").fetchone()[0] == 2
    assert api.records.db.execute("SELECT COUNT(*) FROM lagent_artifact_links WHERE record_id IN (SELECT record_id FROM lagent_records WHERE kind='cost')").fetchone()[0] >= 4


def test_concurrent_reservations_cannot_each_take_the_same_remaining_funds(budget):
    api = initialize(budget)
    database = api.records.db.execute('PRAGMA database_list').fetchone()[2]
    barrier = Barrier(2)
    claims = [bound(api, 'main'), bound(api, 'child')]
    def run(claim):
        db = sqlite3.connect(database, timeout=10)
        records = ExperimentRecords(db, artifacts=api.records.artifacts, clock=api.records.clock)
        worker = CostBudget(records, api.lease)
        commit = worker._commit
        def synchronized(*args, **kwargs):
            barrier.wait(timeout=5)
            return commit(*args, **kwargs)
        worker._commit = synchronized
        try:
            return response(reserve(worker, claim, claim['invocation_id']))['status']
        except Conflict:
            return 'revision_conflict'
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, claims))
    assert sorted(results) == ['reserved', 'revision_conflict']
    assert api.read()['buckets']['research']['held'] == '5.00'
    missing = next(c for c in claims if c['invocation_id'] not in api._state()['value']['invocations'])
    assert response(reserve(api, missing, 'after-reread'))['code'] == 'insufficient_call_budget'


def test_phase_guard_is_rechecked_inside_atomic_commit_and_late_settlement_needs_only_live_owner(budget):
    api = initialize(budget)
    claim = bound(api)
    def closed(): raise Fenced('phase closed')
    with pytest.raises(Fenced): api.reserve(claim, action_id='closed', guard=closed)
    assert not api._state()['value']['invocations']
    reserve(api, claim)
    with pytest.raises(Fenced): api.start('main-1', action_id='closed-start', guard=closed)
    start(api)
    api.settle('main-1', receipt(api, claim), action_id='late')
    assert api.read()['reconciled']


def test_unresolved_or_unproven_envelope_and_free_subscription_are_not_accepted(budget):
    api, table, envelope, _ = budget
    bad = deepcopy(envelope)
    bad['environment_units']['cpu']['seconds'] = '20'
    bad['replay_evidence_hash'] = proof(api, {k:v for k,v in bad.items() if k != 'replay_evidence_hash'})
    with pytest.raises(CostUnavailable, match='exceeds'): api.initialize(table,bad,action_id='too-large')
    bad = deepcopy(table)
    for meter in bad['tariffs'][0]['meters']: meter['usd_per_unit'] = '0'
    with pytest.raises(ValueError, match='subscription'): api.initialize(bad,envelope,action_id='free')
    assert api.records.projection(api.lease.test_id, 'cost_budget') is None


@pytest.mark.parametrize('budget', [{'mode':'calibrated','task_cost_limit':'calibration_derived'}], indirect=True)
def test_calibration_required_mode_does_not_accept_an_explicit_shortcut(budget):
    api, table, envelope, _ = budget
    with pytest.raises(CostUnavailable, match='calibration_required'):
        api.initialize(table, envelope, action_id='initialize')


@pytest.mark.parametrize('point', ['after_record', 'after_projection', 'before_commit', 'after_commit'])
def test_usage_settlement_crash_preserves_hold_or_single_charge(budget, point):
    api = initialize(budget)
    claim = bound(api)
    reserve(api, claim)
    start(api)
    usage = receipt(api, claim)
    def fault(actual):
        if actual == point: raise KeyboardInterrupt('settlement crash')
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): api.settle('main-1', usage, action_id='settle')
    api.records.fault = lambda _: None
    assert api.read()['buckets']['research']['held'] == ('0' if point == 'after_commit' else '5.00')
    api.settle('main-1', usage, action_id='settle')
    assert api.read()['buckets']['research']['settled'] == '2.00'
    assert api.records.rebuild(api.lease)['cost_budget'] == api._state()


def test_new_owner_reconciles_late_usage_without_old_owner_spending_again(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api, claim)
    start(api)
    api.mark_unknown('main-1', action_id='unknown')
    budget[3][0] += timedelta(seconds=10001)
    new_lease = api.records.claim(api.lease.test_id, worker_id='replacement', lease_seconds=1000)
    usage = receipt(api, claim)
    with pytest.raises(Fenced): api.settle('main-1', usage, action_id='old-owner')
    replacement = CostBudget(api.records, new_lease)
    replacement.settle('main-1', usage, action_id='new-owner')
    assert replacement.read()['buckets']['research']['settled'] == '2.00'
    assert replacement.read()['reconciled']


def test_fractional_token_usage_and_absent_active_guard_are_rejected(budget):
    api = initialize(budget)
    claim = bound(api, maximum={'uncached_input':'0.5','cached_input':'0','billable_output':'1'})
    with pytest.raises(CostUnavailable, match='integral'): reserve(api, claim)
    with pytest.raises(ValueError, match='guard'): api.reserve(bound(api), action_id='unguarded', guard=None)
    assert not api._state()['value']['invocations']


def test_small_fee_is_not_rounded_away_under_ambient_decimal_precision(budget):
    api, table, envelope, _ = budget
    table = deepcopy(table)
    table['tariffs'][0]['meters'][1]['usd_per_unit'] = '0.000000000000000000000000000001'
    envelope = deepcopy(envelope)
    envelope['price_table_hash'] = digest(table)
    envelope['replay_evidence_hash'] = proof(api, {k:v for k,v in envelope.items() if k != 'replay_evidence_hash'})
    api.initialize(table, envelope, action_id='initialize')
    too_large = bound(api, maximum={'uncached_input':'10','cached_input':'1','billable_output':'30'})
    result = response(reserve(api, too_large))
    assert result['status'] == 'denied'
    assert result['required'] == '7.000000000000000000000000000001'
    claim = bound(api, 'exact', maximum={'uncached_input':'10','cached_input':'1','billable_output':'20'})
    reserve(api, claim, 'exact-reserve')
    start(api, 'exact')
    usage = receipt(api, claim, units={'uncached_input':'10','cached_input':'1','billable_output':'5'})
    api.settle('exact', usage, action_id='settle')
    assert api.read()['buckets']['research']['settled'] == '2.000000000000000000000000000001'
    assert api.read()['buckets']['research']['remaining'] == '4.999999999999999999999999999999'
