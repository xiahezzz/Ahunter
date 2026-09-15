from decimal import Decimal

import pytest

from advisor.research.experiments.budget import CostBudget, CostUnavailable, PriceTable, ResourceEnvelope
from advisor.research.experiments.calibration import Calibrations, derived_limit
from advisor.research.experiments.clock import PhaseClock, phase_schedule
from advisor.research.experiments.records import Lease
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_registration import registry, register_plan, plan_input
from tests.advisor.research.test_experiment_clock import finish, empty


def artifact(records, value):
    return records.artifacts.put_json(value).content_hash


@pytest.fixture
def calibration(registry):
    service, experiment, definition, _, real = registry
    plan = register_plan(registry, purpose='calibration', plan_id='calibration')
    api = CostBudget(service.records, Lease(plan['tests'][0]['record_id'],'not-started',0))
    semantics = artifact(service.records, {'fixture':True,'semantics':'disjoint billable output or CPU seconds'})
    table = PriceTable.model_validate({'version':api.spec.budget.price_table_ref,'currency':'USD',
        'basis':'comparison_equivalent','evidence_kind':'fixture','source_hash':artifact(service.records,{'fixture':'invented tariff'}),
        'tariffs':[{'resource':api.spec.model.model,'kind':'model','usage_semantics_hash':semantics,
                    'meters':[{'meter_id':'output','unit':'token','usd_per_unit':'0.1'}]},
                   {'resource':'cpu','kind':'compute','usage_semantics_hash':semantics,
                    'meters':[{'meter_id':'seconds','unit':'cpu_second','usd_per_unit':'0.1'}]}]})
    envelopes = []
    for task in api.sealed.tasks:
        data = {'specification_hash':api.sealed.specification_hash,'task_id':task.task_id,'price_table_hash':digest(table),
            'safety_factor':str(api.spec.budget.envelope_safety_factor),'no_model':True,'full_market_replay':True,'full_evaluation':True,
            'environment_units':{'cpu':{'seconds':'2'}},'evaluation_units':{'cpu':{'seconds':'1'}}}
        envelopes.append(ResourceEnvelope.model_validate({**data,'replay_evidence_hash':artifact(service.records,data)}))
    return service,plan,table,envelopes,api.sealed,real


def freeze(fixture):
    service,plan,table,envelopes,_,_ = fixture
    return Calibrations(service.records).freeze(plan['plan']['record_id'],table,envelopes)


def running(fixture, index, campaign):
    service,plan,table,envelopes,_,_ = fixture
    records = service.records
    test = plan['tests'][index]
    test_id = test['record_id']
    records.transition(test_id,'preflight',action_id='preflight')
    records.transition(test_id,'queued',action_id='queued')
    lease = records.claim(test_id,worker_id='worker',lease_seconds=10000)
    records.transition(test_id,'running',action_id='running',lease=lease)
    api = CostBudget(records,lease)
    envelope = next(e for e in envelopes if e.task_id == api.task_id)
    api.initialize(table,envelope,action_id='budget',campaign_id=campaign['record_id'])
    clock = PhaseClock(records,lease)
    clock.create(action_id='clock')
    return api,clock


def invoke(api, phase_id, identity, amount, *, bucket='research', unknown=False, resource=None, actual_amount=None):
    table = api._state()['value']['table']
    resource = resource or (api.spec.model.model if bucket=='research' else 'cpu')
    tariff = next(t for t in table['tariffs'] if t['resource']==resource)
    metric = tariff['meters'][0]['meter_id']
    data = {'invocation_id':identity,'actor_id':'main','phase_id':phase_id,'bucket':bucket,'resource':resource,
        'adapter_ref':'fixture','executor_ref':'fixture','price_table_hash':digest(table),
        'usage_semantics_hash':tariff['usage_semantics_hash'],'maximum_units':{metric:str(amount)}}
    api.reserve({**data,'proof_hash':artifact(api.records,data)},action_id='reserve-'+identity,guard=lambda:None)
    api.start(identity,action_id='start-'+identity,guard=lambda:None)
    if unknown:
        api.mark_unknown(identity,action_id='unknown-'+identity)
        return
    usage = {k:data[k] for k in ('resource','adapter_ref','executor_ref','price_table_hash','usage_semantics_hash')}
    usage.update(units={metric:str(amount if actual_amount is None else actual_amount)},outcome='completed',supplier_bill_usd=None)
    api.settle(identity,{**usage,'evidence_hash':artifact(api.records,{'invocation_id':identity,**usage})},action_id='settle-'+identity)


def complete_test(fixture,index,campaign,*,amount=1,missing_phase=False,unknown=False,resource=None,overrun=False):
    api,clock = running(fixture,index,campaign)
    for i,phase in enumerate(clock.schedule):
        clock.activate(empty(),action_id='activate-'+str(i))
        if not (missing_phase and i==0):
            invoke(api,phase.phase_id,'research-'+str(i),amount,unknown=unknown and i==0,resource=resource)
        finish(clock,str(i))
    invoke(api,clock.schedule[-1].phase_id,'environment',2,bucket='environment',actual_amount=4 if overrun else None)
    invoke(api,clock.schedule[-1].phase_id,'evaluation',1,bucket='evaluation')
    api.records.transition(api.lease.test_id,'evaluating',action_id='evaluating',lease=api.lease)
    api.records.transition(api.lease.test_id,'completed',action_id='completed',lease=api.lease)
    return api


def test_campaign_freezes_entire_task_envelopes_and_baseline_before_execution(calibration):
    first = freeze(calibration)
    assert freeze(calibration) == first
    assert len(first['value']['test_ids']) == 3
    assert set(first['value']['phase_counts']) == {t.task_id for t in calibration[4].tasks}
    assert all(c==16 for c in first['value']['phase_counts'].values())
    assert first['value']['reserves']['august-tuning'] == {'environment':'0.4','evaluation':'0.2'}
    assert not first['value']['formal_ready']
    with pytest.raises(Conflict):
        Calibrations(calibration[0].records).freeze(calibration[1]['plan']['record_id'],calibration[2],calibration[3][:-1])


def test_campaign_cannot_be_replaced_by_another_sample_set(calibration):
    first = freeze(calibration)
    service,plan,table,envelopes,sealed,_ = calibration
    definition = service.records.read(plan['plan']['value']['definition_id'])
    other = service.test_plan(plan['plan']['experiment_id'],definition['record_id'],plan_input(definition,plan_id='other-calibration'),
                              submission_identity='other',purpose='calibration')
    with pytest.raises(Conflict): Calibrations(service.records).freeze(other['plan']['record_id'],table,envelopes)
    assert freeze(calibration) == first


def test_measurement_budget_has_no_comparison_cap_but_reserves_environment(calibration):
    campaign = freeze(calibration)
    api,clock = running(calibration,0,campaign)
    assert api.read()['total_limit'] is None and api.read()['buckets']['research']['remaining'] is None
    assert api.read()['buckets']['environment']['limit'] == '0.4'
    invoke(api,clock.schedule[0].phase_id,'costly-calibration',100000)
    assert api.read()['buckets']['research']['settled'] == '10000.0'
    assert api.spec.model.token_cap is None


def test_three_complete_results_use_maximum_and_formal_test_gets_frozen_allocation(calibration):
    campaign = freeze(calibration)
    for index,amount in enumerate((1,3,2)): complete_test(calibration,index,campaign,amount=amount)
    result = Calibrations(calibration[0].records).complete(campaign['record_id'])
    assert result['value']['maximum_research_cost'] == '4.8'
    assert len(result['value']['measurements']) == 3
    assert all(a['total_limit']=='7' for a in result['value']['allocations'].values())
    assert Calibrations(calibration[0].records).complete(campaign['record_id']) == result
    service,plan,table,envelopes,_,_ = calibration
    definition = service.records.read(plan['plan']['value']['definition_id'])
    formal = service.test_plan(plan['plan']['experiment_id'],definition['record_id'],plan_input(definition,plan_id='formal'),
                               submission_identity='formal',purpose='tuning')['tests'][0]
    records = service.records
    records.transition(formal['record_id'],'preflight',action_id='preflight')
    records.transition(formal['record_id'],'queued',action_id='queued')
    lease = records.claim(formal['record_id'],worker_id='formal',lease_seconds=10000)
    records.transition(formal['record_id'],'running',action_id='running',lease=lease)
    api = CostBudget(records,lease)
    api.initialize(table,envelopes[0],action_id='budget',campaign_id=campaign['record_id'],calibration_result_id=result['record_id'])
    assert api.read()['total_limit'] == '7'
    assert api.read()['buckets']['research']['limit'] == '6.4'
    assert api._state()['value']['allocation']['budget_fingerprint'] == result['value']['budget_fingerprint']
    assert not api.read()['formal_ready']


@pytest.mark.parametrize('cause',['incomplete','unknown','missing_phase','overrun'])
def test_incomplete_or_unknown_repeat_cannot_be_dropped_for_cheaper_calibration(calibration,cause):
    campaign = freeze(calibration)
    complete_test(calibration,0,campaign,amount=1)
    complete_test(calibration,1,campaign,amount=2)
    if cause != 'incomplete':
        complete_test(calibration,2,campaign,amount=3,unknown=cause=='unknown',missing_phase=cause=='missing_phase',overrun=cause=='overrun')
    with pytest.raises(CostUnavailable): Calibrations(calibration[0].records).complete(campaign['record_id'])
    assert calibration[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE json_extract(value_json,'$.cost_record_type')='calibration_result'").fetchone()[0] == 0


def test_compute_only_baseline_does_not_gain_an_invented_minimum_model_call_requirement(calibration):
    campaign = freeze(calibration)
    for index in range(3): complete_test(calibration,index,campaign,amount=1,resource='cpu')
    result = Calibrations(calibration[0].records).complete(campaign['record_id'])
    assert result['value']['maximum_research_cost'] == '1.6'


def test_freeze_after_test_start_is_rejected(calibration):
    records = calibration[0].records
    records.transition(calibration[1]['tests'][0]['record_id'],'preflight',action_id='too-early')
    with pytest.raises(Conflict,match='before'): freeze(calibration)


@pytest.mark.parametrize('point',['after_record','after_artifact_references','before_commit','after_commit'])
def test_campaign_freeze_is_atomic_and_durable_retry_is_identical(calibration,point):
    records = calibration[0].records
    def fault(actual):
        if actual==point: raise KeyboardInterrupt('fixture crash')
    records.fault = fault
    with pytest.raises(KeyboardInterrupt): freeze(calibration)
    records.fault = lambda _:None
    result = freeze(calibration)
    assert freeze(calibration)==result
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE json_extract(value_json,'$.cost_record_type')='calibration_campaign'").fetchone()[0]==1


@pytest.mark.parametrize('cost,multiplier,phases,baseline,reserve,unit,expected',[
    ('4.8','1.25',16,16,'.6','1','7'),
    ('4.8','1.25',7,16,'.6','1','4'),
    ('1','1',1,3,'0','.1','0.4'),
    ('0','1.25',16,16,'.6','1','1'),
    ('0','1.25',16,16,'0','1','0'),
    ('1.000000000000000000000000000001','1',1,1,'0','1','2')])
def test_formula_rounds_once_exactly_and_scales_by_predeclared_phase_counts(cost,multiplier,phases,baseline,reserve,unit,expected):
    assert derived_limit(cost,multiplier,phases,baseline,reserve,unit)==Decimal(expected)


@pytest.mark.parametrize('point',['after_record','before_commit','after_commit'])
def test_calibration_result_commit_crash_never_publishes_partial_allocations(calibration,point):
    campaign = freeze(calibration)
    for index in range(3): complete_test(calibration,index,campaign,amount=index+1)
    records = calibration[0].records
    def fault(actual):
        if actual==point: raise KeyboardInterrupt('result crash')
    records.fault = fault
    with pytest.raises(KeyboardInterrupt): Calibrations(records).complete(campaign['record_id'])
    records.fault = lambda _:None
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE json_extract(value_json,'$.cost_record_type')='calibration_result'").fetchone()[0] == int(point=='after_commit')
    result = Calibrations(records).complete(campaign['record_id'])
    assert Calibrations(records).complete(campaign['record_id']) == result
    assert set(result['value']['allocations']) == set(campaign['value']['phase_counts'])


def test_calibration_envelopes_with_hidden_task_scales_are_not_optimizer_feedback(calibration):
    from advisor.research.experiments.queries import ExperimentQueries, QueryDenied
    campaign = freeze(calibration)
    with pytest.raises(QueryDenied): ExperimentQueries(calibration[0].records,viewer='optimizer').detail(campaign['record_id'])


def test_frozen_price_and_resource_evidence_cannot_be_changed_after_measurement_starts(calibration):
    campaign = freeze(calibration)
    api,clock = running(calibration,0,campaign)
    changed = calibration[2].model_dump(mode='json')
    changed['tariffs'][0]['meters'][0]['usd_per_unit'] = '0.01'
    envelopes = []
    for original in calibration[3]:
        data = original.model_dump(mode='json')
        data['price_table_hash'] = digest(changed)
        data.pop('replay_evidence_hash')
        envelopes.append({**data,'replay_evidence_hash':artifact(api.records,data)})
    with pytest.raises(Conflict): Calibrations(api.records).freeze(calibration[1]['plan']['record_id'],changed,envelopes)
    assert freeze(calibration) == campaign


def test_formal_samples_cannot_use_uncapped_measurement_mode_or_missing_result(calibration):
    campaign = freeze(calibration)
    service,plan,table,envelopes,_,_ = calibration
    definition = service.records.read(plan['plan']['value']['definition_id'])
    formal = service.test_plan(plan['plan']['experiment_id'],definition['record_id'],plan_input(definition,plan_id='formal-early'),
                              submission_identity='formal-early',purpose='tuning')['tests'][0]
    records = service.records
    records.transition(formal['record_id'],'preflight',action_id='preflight')
    records.transition(formal['record_id'],'queued',action_id='queued')
    lease = records.claim(formal['record_id'],worker_id='formal',lease_seconds=10000)
    records.transition(formal['record_id'],'running',action_id='running',lease=lease)
    api = CostBudget(records,lease)
    with pytest.raises(CostUnavailable,match='result_required'):
        api.initialize(table,envelopes[0],action_id='budget',campaign_id=campaign['record_id'])
    assert records.projection(formal['record_id'],'cost_budget') is None
