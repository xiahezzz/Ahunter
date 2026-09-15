"""Host cost reconciliation and experiment totals. No model execution or scoring.

Terminal reconciliations are immutable facts layered over the frozen Test budget;
worker leases, lifecycle, market/account projections and prior results stay sealed.
"""
from copy import deepcopy
from decimal import Decimal

from .budget import CallBound, CostBudget, CostUnavailable, PriceTable, UsageReceipt, _json, _sum, priced_receipt
from .records import TERMINAL
from .registration import record_identity
from .repository import Conflict, Missing
from .resolution import digest


def _existing(records, record_id):
    return records.read(record_id) if records.db.execute('SELECT 1 FROM lagent_records WHERE record_id=?',(record_id,)).fetchone() else None


def _apply(records, state, fact):
    """Revalidate each overlay against the exact frozen invocation it reconciles."""
    value = fact['value']
    if state['sequence'] != value['basis_sequence']:
        raise Conflict('late cost fact has a different frozen budget basis')
    invocation = state['value']['invocations'].get(value['invocation_id'])
    if invocation is None or digest(invocation) != value['before_hash']:
        raise Conflict('late cost fact has a different invocation basis')
    if value['request']['operation'] == 'release_unstarted':
        if invocation['status'] != 'reserved':
            raise Conflict('a started invocation cannot be released as unstarted')
        invocation['status'] = 'released_unstarted'
    elif value['request']['operation'] == 'settle':
        receipt = UsageReceipt.model_validate(value['request']['receipt'])
        amount, exceeded = priced_receipt(records,state['value']['table'],invocation,receipt)
        invocation.update(status='settled',receipt=_json(receipt),cost_usd=str(amount),settlement_record_id=fact['record_id'])
        if exceeded:
            code = 'cost_upper_bound_exceeded' if invocation['bound']['bucket']=='research' else 'platform_resource_failure'
            state['value']['failures'].append({'invocation_id':value['invocation_id'],'code':code})
    else:
        raise Conflict('unknown terminal accounting operation')


def effective_budget(records, test_id):
    test = records._test(test_id)
    state = records.projection(test_id,'cost_budget')
    if state is None:
        raise CostUnavailable('budget_not_initialized')
    rows = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='cost' AND json_extract(value_json,'$.cost_record_type')='terminal_reconciliation' AND json_extract(value_json,'$.test_id')=? ORDER BY record_sequence",(test_id,)).fetchall()
    if rows and records.status(test_id) not in TERMINAL:
        raise Conflict('terminal cost facts cannot overlay an active Test')
    refs = []
    for (record_id,) in rows:
        fact = records.read(record_id)
        if fact['experiment_id'] != test['experiment_id'] or {'relation':'test','target_id':test_id} not in records.related(record_id):
            raise Conflict('terminal cost scope mismatch')
        _apply(records,state,fact)
        refs.append(record_id)
    return {**state,'reconciliation_ids':refs,'effective_hash':digest(state['value'])}


class TerminalCosts:
    def __init__(self, records):
        self.records = records

    def settle(self, test_id, invocation_id, receipt, *, guard=None):
        receipt = UsageReceipt.model_validate(receipt)
        return self._write(test_id,invocation_id,{'operation':'settle','receipt':_json(receipt)},(receipt.evidence_hash,),guard=guard)

    def release_unstarted(self, test_id, invocation_id, *, guard=None):
        return self._write(test_id,invocation_id,{'operation':'release_unstarted'},(),guard=guard)

    def _write(self, test_id, invocation_id, request, refs, *, guard=None):
        test = self.records._test(test_id)
        identity = [test_id,invocation_id]
        record_id = record_identity(test['experiment_id'],'cost-reconciliation',identity)
        with self.records._transaction():
            previous = _existing(self.records,record_id)
            if previous:
                if previous['value']['request'] != request:
                    raise Conflict('terminal invocation already has different reconciliation')
                return previous
            if self.records.status(test_id) not in TERMINAL:
                raise Conflict('active Test accounting requires its worker lease')
            state = effective_budget(self.records,test_id)
            invocation = state['value']['invocations'].get(invocation_id)
            if invocation is None:
                raise Conflict('unknown invocation cannot acquire terminal charges')
            value = {'cost_record_type':'terminal_reconciliation','test_id':test_id,'invocation_id':invocation_id,
                'basis_sequence':state['sequence'],'before_hash':digest(invocation),'request':request}
            # Pure validation/reduction before the immutable fact is inserted.
            _apply(self.records,deepcopy(state),{'record_id':record_id,'value':value})
            prepared = self.records.prepare(experiment_id=test['experiment_id'],kind='cost',record_id=record_id,
                submission_identity='terminal-cost:'+digest(identity),value=value,links=(('test',test_id),),artifact_hashes=refs)
            if guard is not None:
                guard()
            return self.records._insert(prepared)


class ProposalCosts:
    """Record outer-proposal attempts independently of task comparison allowances.

    record_started is a durable execution identity, not a task-budget grant. The
    outer executor must obtain its own authorization before dispatch; this class
    calls no external service. Unknown attempts remain unsettled until a receipt.
    """
    def __init__(self, records, experiment_id):
        self.records, self.experiment_id = records, experiment_id
        if records.db.execute('SELECT 1 FROM lagent_experiments WHERE experiment_id=?',(experiment_id,)).fetchone() is None:
            raise Missing('unknown experiment')

    def _id(self, kind, invocation_id):
        return record_identity(self.experiment_id,kind,invocation_id)

    def record_started(self, table, bound, *, parent_candidate_id=None):
        table, bound = PriceTable.model_validate(table), CallBound.model_validate(bound)
        if bound.bucket != 'research':
            raise Conflict('proposal work cannot consume an Episode resource reserve')
        tariff = CostBudget._tariff(table,bound.resource)
        if bound.price_table_hash != digest(table) or bound.usage_semantics_hash != tariff.usage_semantics_hash:
            raise Conflict('proposal price or usage semantics mismatch')
        claim = _json(bound)
        claim.pop('proof_hash')
        if self.records.artifacts.read_json(bound.proof_hash) != claim:
            raise CostUnavailable('cost_capability_missing')
        amount = CostBudget._price(table,bound.resource,bound.maximum_units)
        links = ()
        if parent_candidate_id is not None:
            parent = self.records.read(parent_candidate_id)
            if parent['experiment_id'] != self.experiment_id or parent['kind'] != 'candidate_proposal':
                raise Conflict('proposal parent belongs to another experiment or kind')
            links = (('candidate',parent_candidate_id),)
        value = {'cost_record_type':'proposal_started','table':_json(table),
            'invocation':{'bound':_json(bound),'status':'started','maximum_cost_usd':str(amount),'receipt':None,'cost_usd':None}}
        return self.records.put(experiment_id=self.experiment_id,kind='cost',
            record_id=self._id('proposal-cost-start',bound.invocation_id),submission_identity='proposal-start:'+bound.invocation_id,
            value=value,links=links,artifact_hashes=(table.source_hash,*(t.usage_semantics_hash for t in table.tariffs),bound.proof_hash))

    def settle(self, invocation_id, receipt):
        receipt = UsageReceipt.model_validate(receipt)
        start = self.records.read(self._id('proposal-cost-start',invocation_id))
        amount, exceeded = priced_receipt(self.records,start['value']['table'],start['value']['invocation'],receipt)
        return self.records.put(experiment_id=self.experiment_id,kind='cost',
            record_id=self._id('proposal-cost-settle',invocation_id),submission_identity='proposal-settle:'+invocation_id,
            value={'cost_record_type':'proposal_settled','start_id':start['record_id'],'receipt':_json(receipt),
                   'cost_usd':str(amount),'upper_bound_exceeded':exceeded},
            links=(('source',start['record_id']),),artifact_hashes=(receipt.evidence_hash,))


def _bucket():
    return {'settled_usd':Decimal('0'),'held_upper_usd':Decimal('0'),'unsettled_invocations':0,
            'reserved_invocations':0,'started_invocations':0,'invoice_known_usd':Decimal('0'),'invoice_unknown_invocations':0}


def _accumulate(total, invocation):
    status = invocation['status']
    if status == 'settled':
        total['settled_usd'] = _sum((total['settled_usd'],invocation['cost_usd']))
        total['started_invocations'] += 1
        bill = invocation['receipt']['supplier_bill_usd']
        if bill is None:
            total['invoice_unknown_invocations'] += 1
        else:
            total['invoice_known_usd'] = _sum((total['invoice_known_usd'],bill))
    elif status in ('started','unsettled','reserved'):
        total['held_upper_usd'] = _sum((total['held_upper_usd'],invocation['maximum_cost_usd']))
        if status == 'reserved':
            total['reserved_invocations'] += 1
        else:
            total['unsettled_invocations'] += 1
            total['started_invocations'] += 1
            total['invoice_unknown_invocations'] += 1
    elif status != 'released_unstarted':
        raise Conflict('unrecognized invocation cost state')


def _serialize(value):
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,dict): return {k:_serialize(v) for k,v in value.items()}
    if isinstance(value,list): return [_serialize(v) for v in value]
    return value


class ExperimentCosts:
    """Host owner totals, never a candidate/optimizer data product.

    One database snapshot, one row per Test execution identity or proposal
    attempt. Comparison references and calibration summaries add no executions.
    """
    def __init__(self, records):
        self.records = records

    def read(self, experiment_id):
        if self.records.db.in_transaction:
            raise Conflict('cost overview requires its own read snapshot')
        self.records.db.execute('BEGIN')
        try:
            return self._read(experiment_id)
        finally:
            self.records.db.rollback()

    def _read(self, experiment_id):
        if self.records.db.execute('SELECT 1 FROM lagent_experiments WHERE experiment_id=?',(experiment_id,)).fetchone() is None:
            raise Missing('unknown experiment')
        total, by_purpose, by_bucket = _bucket(), {}, {}
        failures, missing, executions = [], [], set()
        def add(identity,invocation,purpose):
            if identity in executions:
                raise Conflict('duplicate physical execution identity')
            executions.add(identity)
            for group in (total,by_purpose.setdefault(purpose,_bucket()),by_bucket.setdefault(invocation['bound']['bucket'],_bucket())):
                _accumulate(group,invocation)
        rows = self.records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='test' ORDER BY record_sequence",(experiment_id,)).fetchall()
        for (test_id,) in rows:
            test = self.records._test(test_id)
            if self.records.projection(test_id,'cost_budget') is None:
                # A created/preflight-rejected Test has no executed work. Any
                # recorded running transition without a budget is an evidence gap.
                ran = self.records.db.execute("SELECT 1 FROM lagent_events WHERE test_id=? AND kind='status_transition' AND json_extract(value_json,'$.updates[0].value.status')='running' LIMIT 1",(test_id,)).fetchone()
                if ran: missing.append(test_id)
                continue
            state = effective_budget(self.records,test_id)['value']
            for invocation_id, invocation in state['invocations'].items():
                add(digest(['test',test_id,invocation_id]),invocation,test['value']['purpose'])
            failures.extend({'test_id':test_id,**failure} for failure in state['failures'])
        starts = self.records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='cost' AND json_extract(value_json,'$.cost_record_type')='proposal_started' ORDER BY record_sequence",(experiment_id,)).fetchall()
        for (start_id,) in starts:
            start = self.records.read(start_id)
            invocation = start['value']['invocation']
            identity = invocation['bound']['invocation_id']
            settlement = _existing(self.records,record_identity(experiment_id,'proposal-cost-settle',identity))
            if settlement:
                receipt = UsageReceipt.model_validate(settlement['value']['receipt'])
                amount,exceeded = priced_receipt(self.records,start['value']['table'],invocation,receipt)
                if settlement['value']['start_id'] != start_id or Decimal(settlement['value']['cost_usd']) != amount:
                    raise Conflict('proposal settlement basis mismatch')
                invocation.update(status='settled',cost_usd=str(amount),receipt=_json(receipt))
                if exceeded: failures.append({'proposal_invocation_id':identity,'code':'cost_upper_bound_exceeded'})
            add(digest(['proposal',experiment_id,identity]),invocation,'proposal')
        reconciled = not (missing or total['unsettled_invocations'] or total['reserved_invocations'])
        return _serialize({'currency':'USD','basis':'comparison_equivalent','total':total,'by_purpose':by_purpose,
            'by_bucket':by_bucket,'test_count':len(rows),'proposal_attempt_count':len(starts),
            'reconciled':reconciled,'accounting_valid':reconciled and not failures,'missing_cost_tests':missing,
            'failures':failures,'formal_ready':False})
