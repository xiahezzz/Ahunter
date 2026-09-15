"""Predeclared baseline calibration and task-budget derivation, without execution.

Complete fixture episodes exercise these contracts but never confer real provider
or data readiness. The Episode runner still owns actual replay/valuation completion.
"""
from decimal import Decimal
from fractions import Fraction

from .budget import CostBudget, CostUnavailable, PriceTable, ResourceEnvelope, _json, _multiply, _remaining, _sum, resource_reserves
from .clock import phase_schedule
from .candidates import read_candidate
from .costs import effective_budget
from .contracts import ResolvedSpecification
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, verify_specification


def _typed(records, record_id, subtype):
    record = records.read(record_id)
    if record['kind'] != 'cost' or record['value'].get('cost_record_type') != subtype:
        raise Conflict('wrong calibration record type')
    return record


def derived_limit(research_cost, multiplier, phases, baseline_phases, reserve, rounding_unit):
    """Exact rational scaling followed by a single configured upward rounding."""
    if type(phases) is not int or type(baseline_phases) is not int or phases <= 0 or baseline_phases <= 0:
        raise ValueError('positive phase counts required')
    cost, factor, env, unit = map(Decimal, map(str, (research_cost, multiplier, reserve, rounding_unit)))
    if not all(x.is_finite() for x in (cost, factor, env, unit)) or cost < 0 or env < 0 or factor <= 0 or unit <= 0:
        raise ValueError('invalid calibrated cost parameters')
    quanta = (Fraction(cost) * Fraction(factor) * phases / baseline_phases + Fraction(env)) / Fraction(unit)
    count = -(-quanta.numerator // quanta.denominator)
    return _multiply(unit, count)


class Calibrations:
    def __init__(self, records):
        self.records = records

    def freeze(self, plan_id, table, envelopes, *, expected_selection_id=None):
        plan = self.records.read(plan_id)
        if plan['kind'] != 'test_plan' or plan['value'].get('registration_version') != 1 or plan['value']['purpose'] != 'calibration':
            raise Conflict('calibration requires its predeclared test plan')
        definition = self.records.read(plan['value']['definition_id'])
        sealed = ResolvedSpecification.model_validate(definition['value']['specification'])
        spec = verify_specification(sealed)
        if spec.budget.mode != 'calibrated':
            raise CostUnavailable('calibrated_specification_required')
        table = PriceTable.model_validate(table)
        envelopes = tuple(ResourceEnvelope.model_validate(item) for item in envelopes)
        if len(envelopes) != len(sealed.tasks) or {e.task_id for e in envelopes} != {t.task_id for t in sealed.tasks}:
            raise Conflict('all predeclared tasks require resource envelopes before calibration')
        tests = []
        for sample in plan['value']['plan']['tests']:
            test_id = record_identity(plan['experiment_id'], 'test', sample['test_id'])
            test = self.records._test(test_id)
            if test['value']['plan_id'] != plan_id or test['value']['sample'] != sample or test['value']['purpose'] != 'calibration':
                raise Conflict('calibration sample binding mismatch')
            tests.append(test)
        proposals = {t['value']['sample']['proposal_id'] for t in tests}
        tasks = {t['value']['task_id'] for t in tests}
        if (len(proposals) != 1 or len(tasks) != 1 or len(tests) != spec.budget.calibration_repeats
                or {t['value']['sample']['repeat_index'] for t in tests} != set(range(spec.budget.calibration_repeats))):
            raise Conflict('calibration must retain the complete planned repeat set')
        baseline_id = record_identity(plan['experiment_id'], 'candidate', next(iter(proposals)))
        baseline = self.records.read(baseline_id)
        if baseline['value']['proposal']['parent_proposal_id'] is not None:
            raise Conflict('calibration requires a root initial baseline')
        read_candidate(baseline['value']['proposal']['package_hash'], artifacts=self.records.artifacts)
        tuning_task = next(iter(tasks))
        if next(t for t in spec.tasks if t.task_id == tuning_task).role != 'tuning':
            raise Conflict('calibration may not use holdout or selection observations')
        reserves = {e.task_id: {k: str(v) for k,v in resource_reserves(self.records, sealed, e.task_id, table, e).items()}
                    for e in envelopes}
        campaign_id = record_identity(plan['experiment_id'], 'cost-campaign', definition['record_id'])
        value = {'cost_record_type': 'calibration_campaign', 'definition_id': definition['record_id'],
            'specification_hash': sealed.specification_hash, 'plan_id': plan_id, 'baseline_id': baseline_id,
            'baseline_package_hash': baseline['value']['proposal']['package_hash'], 'tuning_task_id': tuning_task,
            'test_ids': [t['record_id'] for t in tests], 'table': _json(table), 'price_table_hash': digest(table),
            'envelopes': {e.task_id: _json(e) for e in sorted(envelopes, key=lambda e:e.task_id)},
            'reserves': reserves, 'phase_counts': {t.task_id: len(phase_schedule(sealed,t.task_id)) for t in sealed.tasks},
            'formula': {'multiplier': str(spec.budget.calibration_multiplier), 'rounding_unit': str(spec.budget.rounding_unit),
                        'repeats': spec.budget.calibration_repeats}, 'formal_ready': False}
        selection_links = ()
        if expected_selection_id is not None:
            selection = self.records.read(expected_selection_id)
            if (selection['experiment_id'] != plan['experiment_id'] or selection['kind'] != 'selection'
                    or selection['value'].get('selection_version') != 1
                    or selection['value'].get('operation') != 'initialize'
                    or selection['value'].get('definition_id') != definition['record_id']
                    or selection['value'].get('initial_baseline_id') != baseline_id):
                raise Conflict('calibration requires the original initialized baseline and definition')
            value['selection_id'] = expected_selection_id
            selection_links = (('selection', expected_selection_id),)
        refs = [table.source_hash, *(t.usage_semantics_hash for t in table.tariffs), *(e.replay_evidence_hash for e in envelopes)]
        prepared = self.records.prepare(experiment_id=plan['experiment_id'], kind='cost', record_id=campaign_id,
            submission_identity='calibration-campaign:' + definition['record_id'], value=value,
            links=(('target',definition['record_id']),('plan',plan_id),('candidate',baseline_id), *selection_links), artifact_hashes=refs)
        with self.records._transaction():
            exists = self.records.db.execute('SELECT 1 FROM lagent_records WHERE record_id=?',(campaign_id,)).fetchone()
            if not exists:
                from .selection import current_selection, require_selection_open
                require_selection_open(self.records, plan['experiment_id'])
                if expected_selection_id is not None:
                    current = current_selection(self.records, plan['experiment_id'])
                    if current is None or current['record_id'] != expected_selection_id:
                        raise Conflict('calibration selection revision changed')
                # Guard while holding the same SQLite writer lock as Test starts.
                for (test_id,) in self.records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='test' AND json_extract(value_json,'$.definition_id')=?",
                                                        (plan['experiment_id'],definition['record_id'])).fetchall():
                    if self.records.status(test_id) != 'created' or self.records.projection(test_id,'cost_budget') is not None:
                        raise Conflict('calibration conditions must freeze before any Test starts')
            return self.records._insert(prepared)

    def complete(self, campaign_id):
        campaign = _typed(self.records,campaign_id,'calibration_campaign')
        result_id = record_identity(campaign['experiment_id'],'cost-calibration',campaign_id)
        # Keep the evidence and final result consistent with a single DB snapshot.
        with self.records._transaction():
            exists = self.records.db.execute('SELECT 1 FROM lagent_records WHERE record_id=?',(result_id,)).fetchone()
            if exists:
                return _typed(self.records,result_id,'calibration_result')
            value = campaign['value']
            definition = self.records.read(value['definition_id'])
            sealed = ResolvedSpecification.model_validate(definition['value']['specification'])
            measurements = []
            for test_id in value['test_ids']:
                test = self.records._test(test_id)
                if test['value']['purpose'] != 'calibration' or test['value'].get('rerun_of') or test['value']['plan_id'] != value['plan_id']:
                    raise Conflict('reruns or non-calibration samples cannot replace planned calibration')
                if self.records.status(test_id) != 'completed':
                    raise CostUnavailable('calibration_incomplete')
                phase = self.records.projection(test_id,'phase_clock')
                schedule = phase_schedule(sealed,test['value']['task_id'])
                if (phase is None or phase['value']['status'] != 'finished' or phase['value']['index'] != len(schedule)
                        or phase['value']['schedule_hash'] != digest(schedule) or phase['value']['blocked']
                        or phase['value']['stop_required'] or phase['value']['research_stopped']):
                    raise CostUnavailable('calibration_phase_coverage_incomplete')
                closed = [self.records.event(row[0]) for row in self.records.db.execute(
                    "SELECT event_sequence FROM lagent_events WHERE test_id=? AND kind='phase_closed' ORDER BY event_sequence",(test_id,)).fetchall()]
                if ([event['phase_id'] for event in closed] != [p.phase_id for p in schedule]
                        or any(next(u['value'] for u in e['value']['updates'] if u['name']=='phase_clock')['close_reason']
                               not in ('completed','phase_timeout') for e in closed)):
                    raise CostUnavailable('calibration_phase_coverage_incomplete')
                budget = self.records.projection(test_id,'cost_budget')
                if budget is None or budget['value'].get('allocation',{}).get('campaign_id') != campaign_id:
                    raise CostUnavailable('calibration_costs_missing')
                budget = effective_budget(self.records, test_id)
                state = budget['value']
                if state['price_table_hash'] != value['price_table_hash'] or state['failures']:
                    raise CostUnavailable('calibration_costs_invalid')
                if any(i['status'] not in ('settled','released_unstarted') for i in state['invocations'].values()):
                    raise CostUnavailable('calibration_usage_unsettled')
                # Each research phase must have metered work, which may be
                # compute-only. The protocol imposes no minimum model/token use.
                research_phases = {i['bound']['phase_id'] for i in state['invocations'].values()
                                   if i['status']=='settled' and i['bound']['bucket']=='research'}
                if research_phases != {p.phase_id for p in schedule}:
                    raise CostUnavailable('calibration_research_cost_coverage_incomplete')
                totals = CostBudget._totals(state)
                for bucket in ('environment','evaluation'):
                    if not any(i['bound']['bucket']==bucket and i['status']=='settled' for i in state['invocations'].values()):
                        raise CostUnavailable('calibration_resource_costs_missing')
                measurements.append({'test_id':test_id, 'cost_sequence':budget['sequence'], 'cost_hash':digest(state),
                    'phase_sequence':phase['sequence'], 'phase_hash':digest(phase['value']),
                    'reconciliation_ids':budget['reconciliation_ids'],
                    'research_cost':str(totals['research']['settled']),
                    'environment_cost':str(totals['environment']['settled']), 'evaluation_cost':str(totals['evaluation']['settled'])})
            maximum = max(Decimal(m['research_cost']) for m in measurements)
            allocations = {}
            for task_id,count in value['phase_counts'].items():
                reserves = {k:Decimal(v) for k,v in value['reserves'][task_id].items()}
                total = derived_limit(maximum,value['formula']['multiplier'],count,value['phase_counts'][value['tuning_task_id']],
                                      _sum(reserves.values()),value['formula']['rounding_unit'])
                limits = {**reserves,'research':_remaining(total,*reserves.values())}
                allocations[task_id] = {'total_limit':str(total),'limits':{k:str(v) for k,v in limits.items()}}
            result = {'cost_record_type':'calibration_result','campaign_id':campaign_id,'campaign_hash':campaign['content_hash'],
                'maximum_research_cost':str(maximum),'measurements':measurements,'allocations':allocations,'formal_ready':False}
            result['budget_fingerprint'] = digest(result)
            prepared = self.records.prepare(experiment_id=campaign['experiment_id'],kind='cost',record_id=result_id,
                submission_identity='calibration-result:'+campaign_id,value=result,
                links=(('source',campaign_id),*(('test',t) for t in value['test_ids']),
                       *(('source',r) for m in measurements for r in m['reconciliation_ids'])))
            return self.records._insert(prepared)


def allocation_for(records, test_id, table, envelope, *, campaign_id, result_id):
    if campaign_id is None:
        raise CostUnavailable('calibration_required')
    test = records._test(test_id)
    campaign = _typed(records,campaign_id,'calibration_campaign')
    value = campaign['value']
    if (test['experiment_id'] != campaign['experiment_id'] or test['value']['definition_id'] != value['definition_id']
            or digest(table) != value['price_table_hash'] or _json(envelope) != value['envelopes'].get(test['value']['task_id'])):
        raise Conflict('calibration allocation conditions mismatch')
    if test['value']['purpose'] == 'calibration':
        if result_id is not None or test_id not in value['test_ids'] or test['value'].get('rerun_of'):
            raise Conflict('only the original frozen calibration samples can initialize measurement budgets')
        # Calibration measures an uncapped research baseline; fixed stage deadlines
        # and per-call proven bounds still apply. This is never a comparison limit.
        return {'campaign_id':campaign_id,'result_id':None,'mode':'calibration_measurement',
                'total_limit':None,'limits':{**value['reserves'][test['value']['task_id']],'research':None}}
    if result_id != record_identity(campaign['experiment_id'],'cost-calibration',campaign_id):
        raise CostUnavailable('calibration_result_required')
    result = _typed(records,result_id,'calibration_result')
    if result['value']['campaign_hash'] != campaign['content_hash']:
        raise Conflict('calibration result campaign mismatch')
    return {'campaign_id':campaign_id,'result_id':result_id,'mode':'calibrated',
            'budget_fingerprint':result['value']['budget_fingerprint'],**result['value']['allocations'][test['value']['task_id']]}
