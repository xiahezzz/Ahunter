from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from advisor.research.experiments.costs import TerminalCosts, ProposalCosts, ExperimentCosts, effective_budget
from advisor.research.experiments.records import ExperimentRecords, Fenced
from advisor.research.experiments.repository import Conflict
from tests.advisor.research.test_experiment_budget import budget, registry, initialize, bound, reserve, start, receipt
from tests.advisor.research.test_experiment_calibration import calibration


def terminal(api, status='cancelled'):
    if status=='completed': api.records.transition(api.lease.test_id,'evaluating',action_id='evaluating',lease=api.lease)
    api.records.transition(api.lease.test_id,status,action_id='terminal',lease=api.lease)


def totals(api):
    return ExperimentCosts(api.records).read(api.experiment_id)


def test_late_cancelled_usage_is_charged_without_reopening_or_rewriting_test(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    api.mark_unknown('main-1',action_id='unknown')
    terminal(api)
    original = api._state()
    before_events = api.records.db.execute('SELECT COUNT(*) FROM lagent_events').fetchone()[0]
    reconciler = TerminalCosts(api.records)
    usage = receipt(api,claim,outcome='cancelled')
    fact = reconciler.settle(api.lease.test_id,'main-1',usage)
    assert reconciler.settle(api.lease.test_id,'main-1',usage)==fact
    assert api.records.status(api.lease.test_id)=='cancelled'
    assert api._state()==original
    assert api.records.db.execute('SELECT COUNT(*) FROM lagent_events').fetchone()[0]==before_events
    assert api.read()['buckets']['research']['settled']=='2.00'
    assert api.read()['buckets']['research']['held']=='0'
    assert totals(api)['total']['settled_usd']=='2.00'
    assert totals(api)['reconciled']
    with pytest.raises(Fenced): api.settle('main-1',usage,action_id='stale-worker')


def test_unknown_terminal_cost_is_never_reported_as_a_complete_zero(budget):
    api = initialize(budget)
    reserve(api,bound(api))
    start(api)
    terminal(api,'failed')
    result = totals(api)
    assert result['total']['settled_usd']=='0'
    assert result['total']['held_upper_usd']=='5.00'
    assert result['total']['unsettled_invocations']==1
    assert not result['reconciled'] and not result['accounting_valid']
    with pytest.raises(Conflict): TerminalCosts(api.records).release_unstarted(api.lease.test_id,'main-1')


def test_terminal_unstarted_reservation_releases_only_its_hold(budget):
    api = initialize(budget)
    reserve(api,bound(api))
    terminal(api)
    assert totals(api)['total']['reserved_invocations']==1
    fact = TerminalCosts(api.records).release_unstarted(api.lease.test_id,'main-1')
    assert TerminalCosts(api.records).release_unstarted(api.lease.test_id,'main-1')==fact
    result = totals(api)
    assert result['total']['held_upper_usd']=='0'
    assert result['total']['started_invocations']==0
    assert result['reconciled']


def test_terminal_reconciliation_cannot_bypass_an_active_worker_or_change_definitive_usage(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    usage = receipt(api,claim)
    reconciler = TerminalCosts(api.records)
    with pytest.raises(Conflict,match='active'): reconciler.settle(api.lease.test_id,'main-1',usage)
    api.settle('main-1',usage,action_id='settle')
    terminal(api,'completed')
    with pytest.raises(Conflict,match='started'): reconciler.settle(api.lease.test_id,'main-1',usage)
    assert totals(api)['total']['settled_usd']=='2.00'


def test_conflicting_late_receipt_and_unknown_invocation_cannot_modify_total(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    terminal(api)
    reconciler = TerminalCosts(api.records)
    reconciler.settle(api.lease.test_id,'main-1',receipt(api,claim))
    with pytest.raises(Conflict): reconciler.settle(api.lease.test_id,'main-1',receipt(api,claim,bill='4'))
    with pytest.raises(Conflict): reconciler.settle(api.lease.test_id,'missing',receipt(api,claim))
    assert totals(api)['total']['settled_usd']=='2.00'


@pytest.mark.parametrize('point',['after_record','after_artifact_references','before_commit','after_commit'])
def test_late_cost_fact_and_evidence_commit_atomically(budget,point):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    terminal(api)
    usage = receipt(api,claim)
    def fault(actual):
        if actual==point: raise KeyboardInterrupt('late cost crash')
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): TerminalCosts(api.records).settle(api.lease.test_id,'main-1',usage)
    api.records.fault = lambda _:None
    assert totals(api)['reconciled'] == (point=='after_commit')
    TerminalCosts(api.records).settle(api.lease.test_id,'main-1',usage)
    assert totals(api)['total']['settled_usd']=='2.00'
    assert len(effective_budget(api.records,api.lease.test_id)['reconciliation_ids'])==1


def test_two_reconcilers_record_the_same_external_result_once(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    terminal(api)
    usage = receipt(api,claim)
    database = api.records.db.execute('PRAGMA database_list').fetchone()[2]
    def reconcile(_):
        db = sqlite3.connect(database,timeout=10)
        try:
            records = ExperimentRecords(db,artifacts=api.records.artifacts,clock=api.records.clock)
            return TerminalCosts(records).settle(api.lease.test_id,'main-1',usage)['record_id']
        finally: db.close()
    with ThreadPoolExecutor(max_workers=2) as pool: ids = list(pool.map(reconcile,range(2)))
    assert ids[0]==ids[1]
    assert totals(api)['total']['started_invocations']==1
    assert totals(api)['total']['settled_usd']=='2.00'


def test_late_overrun_preserves_actual_cost_and_invalidates_accounting(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    terminal(api)
    TerminalCosts(api.records).settle(api.lease.test_id,'main-1',receipt(api,claim,units={'uncached_input':'10','cached_input':'0','billable_output':'40'}))
    result = totals(api)
    assert result['total']['settled_usd']=='9.00'
    assert result['reconciled'] and not result['accounting_valid']
    assert result['failures'][0]['code']=='cost_upper_bound_exceeded'
    assert api.records.status(api.lease.test_id)=='cancelled'


def test_missing_budget_for_executed_test_is_an_evidence_gap(budget):
    api = budget[0]
    result = totals(api)
    assert result['missing_cost_tests']==[api.lease.test_id]
    assert not result['accounting_valid']


def test_proposal_failures_retries_and_test_execution_are_separate_and_not_double_counted(budget):
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    api.settle('main-1',receipt(api,claim,bill='3'),action_id='settle')
    proposal = ProposalCosts(api.records,api.experiment_id)
    table = api._state()['value']['table']
    first = proposal.record_started(table,claim)
    assert proposal.record_started(table,claim)==first
    usage = receipt(api,claim,outcome='failed',bill='4')
    settled = proposal.settle('main-1',usage)
    assert proposal.settle('main-1',usage)==settled
    proposal.record_started(table,bound(api,'attempt-2'))
    result = totals(api)
    assert result['total']['settled_usd']=='4.00'
    assert result['total']['invoice_known_usd']=='7'
    assert result['by_purpose']['proposal']['settled_usd']=='2.00'
    assert result['proposal_attempt_count']==2 and result['total']['unsettled_invocations']==1
    assert not result['reconciled']
    # Comparison reuse points to the existing execution; it adds no billing row.
    for index in range(2):
        api.records.put(experiment_id=api.experiment_id,kind='comparison',record_id='comparison-'+str(index),
            submission_identity='comparison-'+str(index),value={'baseline_test_id':api.lease.test_id},links=(('test',api.lease.test_id),))
    assert totals(api)==result
    with pytest.raises(Conflict): proposal.settle('main-1',receipt(api,claim,bill='9'))


@pytest.mark.parametrize('point',['after_record','before_commit','after_commit'])
def test_proposal_settlement_has_one_cost_despite_response_crash(budget,point):
    api = initialize(budget)
    proposal = ProposalCosts(api.records,api.experiment_id)
    claim = bound(api)
    proposal.record_started(api._state()['value']['table'],claim)
    usage = receipt(api,claim,outcome='failed')
    def fault(actual):
        if actual==point: raise KeyboardInterrupt('proposal crash')
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): proposal.settle('main-1',usage)
    api.records.fault = lambda _:None
    proposal.settle('main-1',usage)
    assert totals(api)['by_purpose']['proposal']['settled_usd']=='2.00'
    assert totals(api)['total']['started_invocations']==1



def test_late_calibration_usage_unblocks_derivation_and_totals_include_every_repeat(calibration):
    from advisor.research.experiments.calibration import Calibrations
    from advisor.research.experiments.budget import CostUnavailable
    from tests.advisor.research.test_experiment_calibration import freeze, complete_test, artifact
    campaign = freeze(calibration)
    complete_test(calibration,0,campaign,amount=1)
    complete_test(calibration,1,campaign,amount=2)
    api = complete_test(calibration,2,campaign,amount=3,unknown=True)
    with pytest.raises(CostUnavailable): Calibrations(api.records).complete(campaign['record_id'])
    invocation = api._state()['value']['invocations']['research-0']
    claim = invocation['bound']
    usage = {k:claim[k] for k in ('resource','adapter_ref','executor_ref','price_table_hash','usage_semantics_hash')}
    usage.update(units=claim['maximum_units'],outcome='completed',supplier_bill_usd=None)
    usage['evidence_hash'] = artifact(api.records,{'invocation_id':'research-0',**usage})
    fact = TerminalCosts(api.records).settle(api.lease.test_id,'research-0',usage)
    result = Calibrations(api.records).complete(campaign['record_id'])
    assert result['value']['maximum_research_cost']=='4.8'
    assert fact['record_id'] in result['value']['measurements'][2]['reconciliation_ids']
    before = totals(api)
    assert before['total']['settled_usd']=='10.5'
    assert before['by_purpose']['calibration']['started_invocations']==54
    assert before['accounting_valid'] and not before['formal_ready']
    Calibrations(api.records).complete(campaign['record_id'])
    assert totals(api)==before



def test_rerun_is_a_new_execution_and_keeps_original_cancelled_cost(budget,registry):
    from advisor.research.experiments.budget import CostBudget
    api = initialize(budget)
    claim = bound(api)
    reserve(api,claim)
    start(api)
    terminal(api)
    TerminalCosts(api.records).settle(api.lease.test_id,'main-1',receipt(api,claim,outcome='cancelled'))
    rerun = registry[0].rerun(api.experiment_id,api.lease.test_id,rerun_identity='retry-run',reason='explicit fixture retry')
    records = api.records
    records.transition(rerun['record_id'],'preflight',action_id='preflight')
    records.transition(rerun['record_id'],'queued',action_id='queued')
    lease = records.claim(rerun['record_id'],worker_id='rerun',lease_seconds=10000)
    records.transition(rerun['record_id'],'running',action_id='running',lease=lease)
    retry = CostBudget(records,lease)
    retry.initialize(budget[1],budget[2],action_id='budget')
    retry_claim = bound(retry)
    reserve(retry,retry_claim)
    start(retry)
    retry.settle('main-1',receipt(retry,retry_claim),action_id='settle')
    result = totals(api)
    assert result['total']['settled_usd']=='4.00'
    assert result['total']['started_invocations']==2


def test_cost_overview_is_scoped_to_one_experiment(budget):
    from advisor.research.experiments.repository import ExperimentStore
    from advisor.research.experiments.contracts import original_case
    api = initialize(budget)
    other = ExperimentStore(api.records.db).create(original_case(),'other-cost-experiment')['experiment_id']
    proposal = ProposalCosts(api.records,other)
    claim = bound(api)
    proposal.record_started(api._state()['value']['table'],claim)
    assert totals(api)['proposal_attempt_count']==0
    assert ExperimentCosts(api.records).read(other)['total']['unsettled_invocations']==1
