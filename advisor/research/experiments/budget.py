"""Host-only USD-equivalent budget ledger; no provider or model execution.

Price/usage/bound evidence is supplied by host adapters. This module verifies
binding and arithmetic, not the truth of a provider's physical limit. Real
capability acceptance and calibration are separate gates.
"""
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Hash, Money, Name, PositiveDecimal, ResolvedSpecification
from .records import ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, verify_specification

ZERO = Decimal('0')
Bucket = Literal['research', 'environment', 'evaluation']


def _parts(value):
    value = Decimal(str(value))
    sign, digits, exponent = value.as_tuple()
    coefficient = int(''.join(map(str, digits))) * (-1 if sign else 1)
    return coefficient, exponent


def _decimal(coefficient, exponent):
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), exponent))


def _sum(values):
    # Construct from decimal coefficients; ambient Decimal precision must never
    # round away a fee or turn a tiny overspend into an authorized reservation.
    parts = [_parts(value) for value in values]
    if not parts:
        return ZERO
    exponent = min(e for _, e in parts)
    return _decimal(sum(c * 10 ** (e - exponent) for c, e in parts), exponent)


def _multiply(left, right):
    a, x = _parts(left)
    b, y = _parts(right)
    return _decimal(a * b, x + y)


def _remaining(limit, *charges):
    return _sum([limit, *(Decimal(str(c)).copy_negate() for c in charges)])


class CostUnavailable(ValueError):
    pass


class Meter(Contract):
    meter_id: Name
    unit: Name
    usd_per_unit: Money


class Tariff(Contract):
    resource: Name
    kind: Literal['model', 'search', 'compute']
    # The adapter must normalize overlapping supplier counters into these
    # disjoint billable dimensions, e.g. uncached/cached input, billable output.
    usage_semantics_hash: Hash
    meters: tuple[Meter, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        if len({m.meter_id for m in self.meters}) != len(self.meters):
            raise ValueError('duplicate tariff meter')
        if self.kind == 'model' and not any(m.usd_per_unit > 0 for m in self.meters):
            raise ValueError('subscription model must have a nonzero equivalent tariff')
        return self


class PriceTable(Contract):
    version: Name
    currency: Literal['USD']
    basis: Literal['comparison_equivalent']
    evidence_kind: Literal['fixture', 'provider_verified']
    source_hash: Hash
    tariffs: tuple[Tariff, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        if len({t.resource for t in self.tariffs}) != len(self.tariffs):
            raise ValueError('duplicate priced resource')
        return self


class CallBound(Contract):
    invocation_id: Name
    actor_id: Name
    phase_id: Name
    bucket: Bucket
    resource: Name
    adapter_ref: Name
    executor_ref: Name
    price_table_hash: Hash
    usage_semantics_hash: Hash
    maximum_units: dict[Name, Money]
    # Evidence must bind exactly this call; a guessed average is not a bound.
    proof_hash: Hash


class UsageReceipt(Contract):
    resource: Name
    adapter_ref: Name
    executor_ref: Name
    price_table_hash: Hash
    usage_semantics_hash: Hash
    units: dict[Name, Money]
    outcome: Literal['completed', 'failed', 'cancelled']
    evidence_hash: Hash
    # None means unknown/unavailable invoice, not a zero supplier bill.
    supplier_bill_usd: Money | None


class ResourceEnvelope(Contract):
    specification_hash: Hash
    task_id: Name
    price_table_hash: Hash
    safety_factor: PositiveDecimal
    replay_evidence_hash: Hash
    no_model: Literal[True]
    full_market_replay: Literal[True]
    full_evaluation: Literal[True]
    environment_units: dict[Name, dict[Name, Money]]
    evaluation_units: dict[Name, dict[Name, Money]]


def _json(model):
    return model.model_dump(mode='json')


def priced_receipt(records, table, invocation, receipt):
    """One receipt validation rule for active, terminal and proposal accounting."""
    receipt = UsageReceipt.model_validate(receipt)
    if invocation['status'] not in ('started', 'unsettled'):
        raise Conflict('usage requires a started invocation')
    bound = invocation['bound']
    for key in ('resource', 'adapter_ref', 'executor_ref', 'price_table_hash', 'usage_semantics_hash'):
        if getattr(receipt, key) != bound[key]:
            raise Conflict('receipt does not match invocation')
    claim = _json(receipt)
    claim.pop('evidence_hash')
    if records.artifacts.read_json(receipt.evidence_hash) != {'invocation_id': bound['invocation_id'], **claim}:
        raise Conflict('usage evidence mismatch')
    amount = CostBudget._price(PriceTable.model_validate(table), receipt.resource, receipt.units)
    exceeded = amount > Decimal(invocation['maximum_cost_usd']) or any(
        count > Decimal(bound['maximum_units'][meter]) for meter, count in receipt.units.items())
    return amount, exceeded


def resource_reserves(records, sealed, task_id, table, envelope):
    """Validate a pinned complete measurement and derive E without model results."""
    spec = verify_specification(sealed)
    settings = spec.budget
    if (table.version != settings.price_table_ref or envelope.price_table_hash != digest(table)
            or envelope.specification_hash != sealed.specification_hash or envelope.task_id != task_id
            or envelope.safety_factor != settings.envelope_safety_factor):
        raise Conflict('price or resource envelope does not match sealed conditions')
    records.artifacts.read_json(table.source_hash)
    for tariff in table.tariffs:
        records.artifacts.read_json(tariff.usage_semantics_hash)
    claim = _json(envelope)
    claim.pop('replay_evidence_hash')
    if records.artifacts.read_json(envelope.replay_evidence_hash) != claim:
        raise Conflict('resource envelope evidence mismatch')
    limits = {}
    for bucket in ('environment', 'evaluation'):
        units = getattr(envelope, bucket + '_units')
        if not units:
            raise CostUnavailable('resource_envelope_unresolved')
        if any(CostBudget._tariff(table, resource).kind != 'compute' for resource in units):
            raise CostUnavailable('environment_reserve_is_compute_only')
        amount = _multiply(_sum(CostBudget._price(table, resource, metrics) for resource, metrics in units.items()), envelope.safety_factor)
        configured = getattr(settings, bucket + '_reserve')
        limits[bucket] = amount if configured == 'unresolved' else configured
        if amount > limits[bucket]:
            raise CostUnavailable('resource_envelope_exceeds_reserve')
    return limits


class CostBudget:
    def __init__(self, records, lease):
        self.records, self.lease = records, lease
        test = records._test(lease.test_id)
        if test['value'].get('registration_version') != 1:
            raise Conflict('budget requires a typed Test')
        definition = records.read(test['value']['definition_id'])
        if definition['kind'] != 'definition' or definition['experiment_id'] != test['experiment_id']:
            raise Conflict('budget definition mismatch')
        self.sealed = ResolvedSpecification.model_validate(definition['value']['specification'])
        self.spec = verify_specification(self.sealed)
        self.task_id = test['value']['task_id']
        self.experiment_id = test['experiment_id']

    def _state(self):
        state = self.records.projection(self.lease.test_id, 'cost_budget')
        if state is None:
            raise CostUnavailable('budget_not_initialized')
        if state['value']['specification_hash'] != self.sealed.specification_hash:
            raise Conflict('budget specification mismatch')
        return state

    def _prior(self, action_id, request):
        row = self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='budget' AND action_id=? AND attempt=0",
                                      (self.lease.test_id, action_id)).fetchone()
        if row is None:
            return None
        event = self.records.event(row[0])
        if event['value']['payload']['request_hash'] != digest(request):
            raise Conflict('budget action has different input')
        return event

    def _commit(self, state, value, request, response, action_id, *, guard=None, extra_updates=()):
        refs = []
        if request['operation'] == 'initialize':
            refs = [request['table']['source_hash'], request['envelope']['replay_evidence_hash'],
                    *(t['usage_semantics_hash'] for t in request['table']['tariffs'])]
        elif request['operation'] == 'reserve':
            refs = [request['bound']['proof_hash']]
        elif request['operation'] == 'settle':
            refs = [request['receipt']['evidence_hash']]
        identity = [self.lease.test_id, action_id]
        prepared = self.records.prepare(experiment_id=self.experiment_id, kind='cost',
            record_id=record_identity(self.experiment_id, 'cost', identity), submission_identity=digest(identity),
            value={'request': request, 'response': response}, links=(('test', self.lease.test_id),
                *((('source', request['campaign_id']),) if request.get('campaign_id') else ()),
                *((('source', request['calibration_result_id']),) if request.get('calibration_result_id') else ())), artifact_hashes=refs)
        def commit_guard():
            if guard is not None:
                guard()
            self.records._insert(prepared)
        return self.records.commit(self.lease, phase_id='budget', action_id=action_id, attempt=0,
            kind='budget_operation', payload={'request_hash': digest(request), 'operation': request['operation'],
                'invocation_id': request.get('invocation_id'), 'response': response}, simulated_at=None,
            updates=(ProjectionUpdate('cost_budget', state['sequence'] if state else None, value), *extra_updates), guard=commit_guard)

    def _evidence(self, content_hash):
        if self.records.artifacts is None:
            raise CostUnavailable('cost_evidence_missing')
        return self.records.artifacts.read_json(content_hash)

    @staticmethod
    def _tariff(table, resource):
        tariff = next((t for t in table.tariffs if t.resource == resource), None)
        if tariff is None:
            raise CostUnavailable('cost_capability_missing')
        return tariff

    @classmethod
    def _price(cls, table, resource, units):
        tariff = cls._tariff(table, resource)
        if set(units) != {m.meter_id for m in tariff.meters}:
            raise CostUnavailable('usage_dimensions_incomplete')
        if any(m.unit == 'token' and Decimal(str(units[m.meter_id])) != Decimal(str(units[m.meter_id])).to_integral_value() for m in tariff.meters):
            raise CostUnavailable('token_usage_must_be_integral')
        return _sum(_multiply(units[m.meter_id], m.usd_per_unit) for m in tariff.meters)

    def initialize(self, table, envelope, *, action_id, campaign_id=None, calibration_result_id=None):
        table, envelope = PriceTable.model_validate(table), ResourceEnvelope.model_validate(envelope)
        request = {'operation': 'initialize', 'table': _json(table), 'envelope': _json(envelope)}
        if campaign_id is not None or calibration_result_id is not None:
            request.update(campaign_id=campaign_id, calibration_result_id=calibration_result_id)
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        if self.records.projection(self.lease.test_id, 'cost_budget') is not None:
            raise Conflict('budget already initialized')
        settings = self.spec.budget
        if settings.mode == 'explicit':
            if campaign_id is not None or calibration_result_id is not None:
                raise Conflict('explicit budget cannot take calibrated allocations')
            if not all(isinstance(v, Decimal) for v in (settings.task_cost_limit, settings.environment_reserve, settings.evaluation_reserve)):
                raise CostUnavailable('resource_envelope_unresolved')
            limits = resource_reserves(self.records, self.sealed, self.task_id, table, envelope)
            total_limit = settings.task_cost_limit
            limits['research'] = _remaining(total_limit, *limits.values())
            allocation = None
        else:
            from .calibration import allocation_for
            allocation = allocation_for(self.records, self.lease.test_id, table, envelope,
                                        campaign_id=campaign_id, result_id=calibration_result_id)
            total_limit = allocation['total_limit']
            limits = {k: Decimal(v) if v is not None else None for k,v in allocation['limits'].items()}
        value = {'specification_hash': self.sealed.specification_hash, 'price_table_hash': digest(table),
                 'table': _json(table), 'envelope': _json(envelope), 'limits': {k: str(v) if v is not None else None for k,v in limits.items()},
                 'total_limit': str(total_limit) if total_limit is not None else None, 'invocations': {}, 'failures': []}
        if allocation is not None:
            value['allocation'] = allocation
        return self._commit(None, value, request, {'status': 'initialized', 'formal_ready': False}, action_id)

    @staticmethod
    def _totals(value):
        totals = {bucket: {'settled': ZERO, 'held': ZERO, 'unsettled': 0} for bucket in value['limits']}
        for invocation in value['invocations'].values():
            bucket = totals[invocation['bound']['bucket']]
            if invocation['status'] == 'settled':
                bucket['settled'] = _sum((bucket['settled'], invocation['cost_usd']))
            elif invocation['status'] != 'released_unstarted':
                bucket['held'] = _sum((bucket['held'], invocation['maximum_cost_usd']))
                bucket['unsettled'] += int(invocation['status'] in ('started', 'unsettled'))
        return totals

    def read(self):
        from .costs import effective_budget
        value = effective_budget(self.records, self.lease.test_id)['value']
        totals = self._totals(value)
        buckets = {name: {'limit': value['limits'][name], 'settled': str(t['settled']), 'held': str(t['held']),
                         'remaining': str(_remaining(value['limits'][name], t['settled'], t['held'])) if value['limits'][name] is not None else None,
                         'unsettled': t['unsettled']} for name,t in totals.items()}
        return {'currency': 'USD', 'basis': 'comparison_equivalent', 'total_limit': value['total_limit'],
                'buckets': buckets, 'failures': value['failures'], 'formal_ready': False,
                'reconciled': all(i['status'] in ('settled', 'released_unstarted') for i in value['invocations'].values())}

    def reserve(self, bound, *, action_id, guard):
        if not callable(guard):
            raise ValueError('active scope guard required')
        bound = CallBound.model_validate(bound)
        request = {'operation': 'reserve', 'invocation_id': bound.invocation_id, 'bound': _json(bound)}
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        state = self._state()
        value = state['value']
        known = value['invocations'].get(bound.invocation_id)
        if known is not None:
            if known['bound'] != _json(bound):
                raise Conflict('invocation identity has different bound')
            # A durable reservation may be retried, but must not dispatch again.
            return self._prior(known['reservation_action_id'], request)
        table = PriceTable.model_validate(value['table'])
        tariff = self._tariff(table, bound.resource)
        if tariff.kind != 'compute' and bound.bucket != 'research':
            raise CostUnavailable('research_cannot_spend_environment_reserve')
        if tariff.kind == 'model' and bound.resource != self.spec.model.model:
            raise CostUnavailable('model_does_not_match_sealed_specification')
        if bound.price_table_hash != value['price_table_hash'] or bound.usage_semantics_hash != tariff.usage_semantics_hash:
            raise Conflict('invocation price or usage semantics mismatch')
        claim = _json(bound)
        claim.pop('proof_hash')
        if self._evidence(bound.proof_hash) != claim:
            raise CostUnavailable('cost_capability_missing')
        amount = self._price(table, bound.resource, bound.maximum_units)
        total = self._totals(value)[bound.bucket]
        remaining = _remaining(value['limits'][bound.bucket], total['settled'], total['held']) if value['limits'][bound.bucket] is not None else None
        if value['failures'] and bound.bucket == 'research':
            response = {'status': 'denied', 'code': 'cost_accounting_invalid'}
        elif remaining is not None and amount > remaining:
            response = {'status': 'denied', 'code': 'insufficient_call_budget', 'remaining': str(remaining), 'required': str(amount)}
        else:
            value['invocations'][bound.invocation_id] = {'bound': _json(bound), 'maximum_cost_usd': str(amount),
                'status': 'reserved', 'reservation_action_id': action_id, 'receipt': None, 'cost_usd': None}
            response = {'status': 'reserved', 'invocation_id': bound.invocation_id, 'maximum_cost_usd': str(amount)}
        return self._commit(state, value, request, response, action_id, guard=guard)

    def _change(self, invocation_id, operation, *, action_id, guard=None, extra_updates=()):
        request = {'operation': operation, 'invocation_id': invocation_id}
        if extra_updates:
            request['atomic_updates_hash'] = digest([vars(u) for u in extra_updates])
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        state = self._state()
        value = state['value']
        invocation = value['invocations'].get(invocation_id)
        if invocation is None:
            raise Conflict('unknown invocation')
        allowed = {'start': ('reserved',), 'unknown': ('started',), 'release_unstarted': ('reserved',)}
        if invocation['status'] not in allowed[operation]:
            raise Conflict('invalid invocation transition')
        invocation['status'] = {'start': 'started', 'unknown': 'unsettled', 'release_unstarted': 'released_unstarted'}[operation]
        return self._commit(state, value, request, {'status': invocation['status'], 'invocation_id': invocation_id}, action_id, guard=guard, extra_updates=extra_updates)

    def start(self, invocation_id, *, action_id, guard, extra_updates=()):
        """Commit before dispatch; only the original successful start may dispatch.

        Recovery must reconcile a started/unknown invocation, never repeat an
        external effect just because its durable start acknowledgement is reused.
        """
        if not callable(guard):
            raise ValueError('active scope guard required')
        return self._change(invocation_id, 'start', action_id=action_id, guard=guard, extra_updates=extra_updates)

    def mark_unknown(self, invocation_id, *, action_id):
        return self._change(invocation_id, 'unknown', action_id=action_id)

    def release_unstarted(self, invocation_id, *, action_id, guard=None):
        """Only an invocation never committed as started can release unused funds."""
        return self._change(invocation_id, 'release_unstarted', action_id=action_id, guard=guard)

    def settle(self, invocation_id, receipt, *, action_id, guard=None):
        receipt = UsageReceipt.model_validate(receipt)
        request = {'operation': 'settle', 'invocation_id': invocation_id, 'receipt': _json(receipt)}
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        state = self._state()
        value = state['value']
        invocation = value['invocations'].get(invocation_id)
        if invocation is None:
            raise Conflict('unknown invocation')
        if invocation['status'] == 'settled':
            if invocation['receipt'] != _json(receipt):
                raise Conflict('settled invocation has different usage')
            return self._prior(invocation['settlement_action_id'], request)
        amount, exceeded = priced_receipt(self.records, value['table'], invocation, receipt)
        bound = invocation['bound']
        invocation.update(status='settled', receipt=_json(receipt), cost_usd=str(amount), settlement_action_id=action_id)
        if exceeded:
            code = 'platform_resource_failure' if bound['bucket'] != 'research' else 'cost_upper_bound_exceeded'
            value['failures'].append({'invocation_id': invocation_id, 'code': code})
        return self._commit(state, value, request,
            {'status': 'settled', 'invocation_id': invocation_id, 'cost_usd': str(amount), 'upper_bound_exceeded': exceeded}, action_id, guard=guard)
