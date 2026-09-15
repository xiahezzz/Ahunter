from datetime import timedelta
import json

import pytest

from advisor.research.experiments.budget import CostBudget, CostUnavailable
from advisor.research.experiments.clock import PhaseClock
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.runtime import ModelInvocations
from tests.advisor.research.test_experiment_budget import budget, registry, initialize
from tests.advisor.research.test_experiment_clock import empty


def artifact(records,value):
    return records.artifacts.put_json(value).content_hash


class Driver:
    def __init__(self,api):
        self.records = api.records
        self.quotes,self.jobs,self.results,self.starts,self.cancelled = {},{},{},[],[]
        self.raise_start = False
        tariff = next(t for t in api._state()['value']['table']['tariffs'] if t['resource']==api.spec.model.model)
        claim = {'adapter_ref':'fixture-adapter','executor_ref':'fixture-executor','model':api.spec.model.model,
            'reasoning_effort':api.spec.model.reasoning_effort,'price_table_hash':api._state()['value']['price_table_hash'],
            'usage_semantics_hash':tariff['usage_semantics_hash'],'proven_cost_bound':True,'host_actions_only':True,
            'single_attempt':True,'reconcilable':True,'fixture':True}
        self.capability = {**claim,'proof_hash':artifact(self.records,claim)}

    def quote(self,request):
        self.quotes[request['invocation_id']] = request
        cap = self.capability
        claim = {'invocation_id':request['invocation_id'],'actor_id':request['actor_id'],'phase_id':request['phase_id'],
            'bucket':'research','resource':request['model'],'adapter_ref':cap['adapter_ref'],'executor_ref':cap['executor_ref'],
            'price_table_hash':cap['price_table_hash'],'usage_semantics_hash':cap['usage_semantics_hash'],
            'maximum_units':{'uncached_input':'0','cached_input':'0','billable_output':'5'}}
        return {**claim,'proof_hash':artifact(self.records,claim)}

    def start(self,request):
        identity = request['invocation_id']
        self.starts.append(identity)
        self.jobs[identity] = request
        if self.raise_start: raise RuntimeError('secret backend /private/host')
        return identity

    def poll(self,handle): return self.results.get(handle)
    def reconcile(self,identity): return self.results.get(identity)
    def cancel(self,identity): self.cancelled.append(identity)

    def done(self,identity,*,status='completed',tokens='1',model=None,effort=None,output=None,quiescent=True,unknown=False,code=None):
        cap = self.capability
        receipt = {'resource':cap['model'],'adapter_ref':cap['adapter_ref'],'executor_ref':cap['executor_ref'],
            'price_table_hash':cap['price_table_hash'],'usage_semantics_hash':cap['usage_semantics_hash'],
            'units':{'uncached_input':'0','cached_input':'0','billable_output':tokens},'outcome':status,'supplier_bill_usd':None}
        receipt['evidence_hash'] = artifact(self.records,{'invocation_id':identity,**receipt})
        self.results[identity] = {'model':model or cap['model'],'reasoning_effort':effort or cap['reasoning_effort'],
            'status':'unknown' if unknown else status,'quiescent':quiescent,'receipt':None if unknown else receipt,
            'output': output if output is not None else {'action':'finish','description':'finish public research',
                'report':'fixture report','evidence':['visible-evidence']},'failure_code':code}


@pytest.fixture
def runtime(budget):
    api = initialize(budget)
    clock = PhaseClock(api.records,api.lease)
    clock.create(action_id='clock')
    clock.activate(empty(),action_id='active')
    driver = Driver(api)
    runtime = ModelInvocations(api,clock,driver,main_actor_id='main',allow_fixture=True)
    return runtime,clock,driver,budget[3]


def begin(fixture,call='call',session=None,attempt=0):
    runtime,clock,_,_ = fixture
    session = session or clock.session('main')
    result = runtime.begin(session,call,{'instructions':'fixture research','context':{}},attempt=attempt)
    return session,result['invocation_id']


def test_one_attempt_is_reserved_started_and_charged_before_action_publication(runtime):
    api,clock,driver,_ = runtime
    session,identity = begin(runtime)
    assert api.budget.read()['buckets']['research']['held']=='1.00'
    assert len(driver.starts)==1
    assert api.poll(session,identity)['status']=='pending'
    driver.done(identity)
    result = api.poll(session,identity)
    assert result['status']=='completed' and result['action']['action']=='finish'
    assert api.budget.read()['buckets']['research']['settled']=='0.20'
    assert api._state()['value']['calls'][identity]['actual_model']=='gpt-5.5'
    assert api.poll(session,identity)==result
    begin(runtime)
    assert len(driver.starts)==1 and not result['formal_ready']


def test_driver_capability_and_fixture_mode_are_explicit(runtime):
    api,clock,driver,_ = runtime
    with pytest.raises(CostUnavailable,match='fixture'): ModelInvocations(api.budget,clock,driver,main_actor_id='main')
    from advisor.research.codex.executor import CodexExecutor
    with pytest.raises(CostUnavailable,match='cost_capability_missing'):
        ModelInvocations(api.budget,clock,CodexExecutor(),main_actor_id='main')
    assert not driver.starts


def test_parent_and_same_named_child_are_distinct_and_root_decision_maker_is_fixed(runtime):
    api,clock,driver,_ = runtime
    root = clock.session('main')
    _,parent = begin(runtime,session=root)
    _,child = begin(runtime,session=root.child('main'))
    assert parent!=child and len(driver.starts)==2
    with pytest.raises(Fenced): begin(runtime,session=clock.session('another-root'))
    with pytest.raises(Fenced): api.poll(root,child)


def test_inflight_child_concurrency_and_single_call_per_actor_are_enforced(runtime):
    api,clock,driver,_ = runtime
    root = clock.session('main')
    session,_ = begin(runtime)
    with pytest.raises(CostUnavailable,match='actor_already'): begin(runtime,call='overlap',session=session)
    for i in range(api.budget.spec.runtime.subagent_concurrency): begin(runtime,session=root.child('child-'+str(i)))
    with pytest.raises(CostUnavailable,match='concurrency'): begin(runtime,session=root.child('too-many'))
    assert len(driver.starts)==5


def test_phase_close_waits_for_quiescence_then_keeps_unknown_cost_until_reconciled(runtime):
    api,clock,driver,_ = runtime
    session,identity = begin(runtime)
    clock.begin_close('completed',action_id='closing')
    with pytest.raises(Conflict,match='cleanup'): clock.finish_close(action_id='closed')
    assert api.stop_phase(session.scope.phase_id)['status']=='stopping'
    driver.done(identity,unknown=True)
    assert api.stop_phase(session.scope.phase_id)['status']=='stopped'
    assert api.budget.read()['buckets']['research']['held']=='1.00'
    clock.finish_close(action_id='closed')
    clock.advance(action_id='next')
    clock.activate(empty(),action_id='next-active')
    driver.done(identity)
    assert api.reconcile(identity)['status']=='discarded'
    assert api.budget.read()['buckets']['research']['settled']=='0.20'
    with pytest.raises(Fenced): api.poll(clock.session('main'),identity)
    assert api._state()['value']['calls'][identity]['action_hash'] is None


def test_cancelled_late_result_is_billed_and_never_becomes_an_action(runtime):
    api,clock,driver,_ = runtime
    session,identity = begin(runtime)
    clock.begin_close('cancelled',action_id='closing')
    driver.done(identity)
    result = api.poll(session,identity)
    assert result['status']=='discarded' and 'action' not in result
    assert api.budget.read()['buckets']['research']['settled']=='0.20'
    assert driver.cancelled==[identity]
    clock.finish_close(action_id='closed')


def test_call_timeout_requests_stop_and_cleanup_timeout_does_not_claim_process_exit(runtime):
    api,clock,driver,real = runtime
    session,identity = begin(runtime)
    real[0] += timedelta(seconds=api.budget.spec.runtime.timeout_seconds)
    assert api.poll(session,identity)['status']=='pending'
    assert driver.cancelled==[identity]
    real[0] += timedelta(seconds=api.budget.spec.runtime.cancel_wait_seconds)
    assert api.poll(session,identity)['code']=='process_cleanup_timeout'
    assert not api._state()['value']['calls'][identity]['quiescent']
    driver.done(identity,status='cancelled',tokens='0')
    assert api.poll(session,identity)['status']=='discarded'


def test_dispatch_exception_is_unknown_and_retry_cannot_duplicate_external_attempt(runtime):
    api,clock,driver,_ = runtime
    driver.raise_start = True
    session,identity = begin(runtime)
    assert api._state()['value']['calls'][identity]['status']=='unsettled'
    begin(runtime)
    with pytest.raises(Conflict): begin(runtime,attempt=1)
    assert driver.starts==[identity]
    assert api.budget.read()['buckets']['research']['held']=='1.00'
    driver.done(identity)
    assert api.poll(session,identity)['status']=='completed'


def test_only_settled_transport_failure_can_retry_with_a_new_charge(runtime):
    api,clock,driver,_ = runtime
    session,first = begin(runtime)
    driver.done(first,status='failed',code='transport_error')
    assert api.poll(session,first)['status']=='failed'
    _,second = begin(runtime,attempt=1)
    assert first!=second and driver.starts==[first,second]
    driver.done(second)
    assert api.poll(session,second)['status']=='completed'
    assert api.budget.read()['buckets']['research']['settled']=='0.40'
    with pytest.raises(Conflict): begin(runtime,attempt=2)


@pytest.mark.parametrize('variant',['model','effort','schema','overrun'])
def test_wrong_identity_schema_or_cost_overrun_never_publishes_an_action(runtime,variant):
    api,clock,driver,_ = runtime
    session,identity = begin(runtime)
    kwargs = {'model':'wrong-model'} if variant=='model' else {'effort':'low'} if variant=='effort' else {'output':{'secret':'/private/host'}} if variant=='schema' else {'tokens':'8'}
    driver.done(identity,**kwargs)
    result = api.poll(session,identity)
    assert 'action' not in result
    if variant in ('model','effort'):
        assert result['code']=='model_identity_mismatch'
        assert api.budget.read()['buckets']['research']['held']=='1.00'
    else:
        assert result['code']==('candidate_schema_invalid' if variant=='schema' else 'cost_upper_bound_exceeded')
        assert api.budget.read()['buckets']['research']['held']=='0'
    assert '/private/host' not in json.dumps(api._state())


def test_restart_between_start_commit_and_dispatch_reconciles_instead_of_starting_again(runtime):
    api,clock,driver,_ = runtime
    crashed = [False]
    def fault(point):
        if point=='after_commit' and not crashed[0]:
            calls = api._state()['value']['calls']
            if calls and next(iter(calls.values()))['status']=='started':
                crashed[0] = True
                raise KeyboardInterrupt('before external dispatch')
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): begin(runtime)
    api.records.fault = lambda _:None
    identity = next(iter(api._state()['value']['calls']))
    recovered = ModelInvocations(api.budget,clock,driver,main_actor_id='main',allow_fixture=True)
    recovered.begin(clock.session('main'),'call',{'instructions':'fixture research','context':{}})
    assert not driver.starts
    driver.done(identity,status='cancelled',tokens='0')
    assert recovered.reconcile(identity)['status']=='discarded'
    assert api.budget.read()['buckets']['research']['settled']=='0.00'


def test_new_worker_recovers_known_result_without_using_old_worker_scope(runtime):
    api,clock,driver,real = runtime
    session,identity = begin(runtime)
    api.records.renew(api.budget.lease,lease_seconds=1)
    real[0] += timedelta(seconds=2)
    lease = api.records.claim(api.budget.lease.test_id,worker_id='replacement',lease_seconds=10000)
    new_clock = PhaseClock(api.records,lease)
    new_clock.resume(action_id='resume')
    new_budget = CostBudget(api.records,lease)
    recovered = ModelInvocations(new_budget,new_clock,driver,main_actor_id='main',allow_fixture=True)
    driver.done(identity)
    with pytest.raises(Fenced): api.poll(session,identity)
    result = recovered.poll(new_clock.session('main'),identity)
    assert result['status']=='completed' and len(driver.starts)==1


def test_closed_phase_during_result_commit_discards_after_billing(runtime):
    api,clock,driver,_ = runtime
    session,identity = begin(runtime)
    driver.done(identity)
    original = api._commit
    def close_before_result(state,invocation,operation,**kwargs):
        if operation=='result': clock.begin_close('completed',action_id='race-close')
        return original(state,invocation,operation,**kwargs)
    api._commit = close_before_result
    result = api.poll(session,identity)
    assert result['status']=='discarded' and 'action' not in result
    assert api.budget.read()['buckets']['research']['settled']=='0.20'
    clock.finish_close(action_id='closed')



@pytest.mark.parametrize('budget',[{'runtime':{'max_steps':1}}],indirect=True)
def test_sealed_step_limit_counts_decisions_without_turning_retry_into_new_step(runtime):
    api,clock,driver,_ = runtime
    session,first = begin(runtime)
    driver.done(first,status='failed',code='transport_error')
    api.poll(session,first)
    _,retry = begin(runtime,attempt=1)
    driver.done(retry)
    api.poll(session,retry)
    with pytest.raises(CostUnavailable,match='step_limit'): begin(runtime,call='second-decision')
    assert len(driver.starts)==2


@pytest.mark.parametrize('budget',[{'runtime':{'total_subagents':1}}],indirect=True)
def test_sealed_total_children_does_not_reset_when_a_call_finishes(runtime):
    api,clock,driver,_ = runtime
    root = clock.session('main')
    child,identity = begin(runtime,session=root.child('first'))
    driver.done(identity)
    api.poll(child,identity)
    with pytest.raises(CostUnavailable,match='subagent_limit'): begin(runtime,session=root.child('second'))
    assert len(driver.starts)==1


def test_insufficient_call_reserve_never_reaches_the_driver(runtime):
    api,clock,driver,_ = runtime
    for index in range(7):
        session,identity = begin(runtime,call='decision-'+str(index))
        driver.done(identity,tokens='5')
        api.poll(session,identity)
    result = api.begin(clock.session('main'),'no-budget',{'instructions':'fixture','context':{}})
    assert result['status']=='denied' and result['code']=='insufficient_call_budget'
    assert len(driver.starts)==7


def test_concurrent_recovery_cannot_both_dispatch_an_idempotent_start(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import sqlite3
    from advisor.research.experiments.records import ExperimentRecords
    api,clock,driver,_ = runtime
    # Leave a durable prepared call by crashing after its preparation commit.
    hit = [False]
    def fault(point):
        if point=='after_commit' and not hit[0] and api._state()['value']['calls']:
            hit[0]=True
            raise KeyboardInterrupt('prepared before reserve')
    api.records.fault=fault
    with pytest.raises(KeyboardInterrupt): begin(runtime)
    api.records.fault=lambda _:None
    identity,prepared=next(iter(api._state()['value']['calls'].items()))
    api.budget.reserve(prepared['bound'],action_id=identity+':reserve',guard=clock.session('main').check)
    database=api.records.db.execute('PRAGMA database_list').fetchone()[2]
    barrier=Barrier(2)
    def run(_):
        db=sqlite3.connect(database,timeout=10)
        records=ExperimentRecords(db,artifacts=api.records.artifacts,clock=api.records.clock)
        budget=CostBudget(records,api.budget.lease)
        own_clock=PhaseClock(records,api.budget.lease)
        worker=ModelInvocations(budget,own_clock,driver,main_actor_id='main',allow_fixture=True)
        original=budget.start
        def synchronized(*args,**kwargs):
            barrier.wait(timeout=5)
            return original(*args,**kwargs)
        budget.start=synchronized
        try:
            return worker.begin(own_clock.session('main'),'call',{'instructions':'fixture research','context':{}})['status']
        except Conflict:
            return 'conflict'
        finally: db.close()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
    assert sorted(results)==['conflict','started']
    assert len(driver.starts)==1
    assert api.budget.read()['buckets']['research']['held']=='1.00'


def test_invalid_driver_response_does_not_expose_errors_or_claim_cleanup(runtime):
    api,clock,driver,_ = runtime
    session,identity=begin(runtime)
    driver.results[identity]={'quiescent':True,'secret':'/private/host'}
    result=api.poll(session,identity)
    assert result['code']=='driver_response_invalid' and '/private' not in json.dumps(result)
    assert not api._state()['value']['calls'][identity]['quiescent']
    clock.begin_close('completed',action_id='closing')
    with pytest.raises(Conflict,match='cleanup'): clock.finish_close(action_id='closed')



def test_phase_closed_after_dispatch_claim_but_before_driver_start_suppresses_dispatch(runtime):
    api,clock,driver,_ = runtime
    closed=[False]
    def fault(point):
        if point=='after_commit' and not closed[0]:
            calls=api._state()['value']['calls']
            if calls and next(iter(calls.values()))['status']=='started':
                closed[0]=True
                clock.begin_close('cancelled',action_id='close-before-start')
    api.records.fault=fault
    result=api.begin(clock.session('main'),'call',{'instructions':'fixture','context':{}})
    api.records.fault=lambda _:None
    assert result['code']=='dispatch_suppressed_before_start' and not driver.starts
    assert api.budget.read()['buckets']['research']['held']=='1.00'
    clock.finish_close(action_id='closed')
    driver.done(result['invocation_id'],status='cancelled',tokens='0')
    api.reconcile(result['invocation_id'])
    assert api.budget.read()['buckets']['research']['settled']=='0.00'
