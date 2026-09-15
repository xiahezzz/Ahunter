"""Metered host model-attempt lifecycle, with explicit cancellable driver handles.

No candidate source is loaded here. A production driver must provide verified
bounds, one external attempt per start, nonblocking poll/cancel/reconcile and
process-tree quiescence. The legacy Codex executor has no such adapter yet.
"""
from datetime import datetime, timedelta
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from advisor.research.lagent import LAgentAction
from .budget import CallBound, CostUnavailable, UsageReceipt
from .candidates import read_candidate
from .contracts import Contract, Hash, Name
from .records import Fenced, ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, encode


class DriverCapability(Contract):
    adapter_ref: Name
    executor_ref: Name
    model: Name
    reasoning_effort: Name
    price_table_hash: Hash
    usage_semantics_hash: Hash
    proven_cost_bound: Literal[True]
    host_actions_only: Literal[True]
    single_attempt: Literal[True]
    reconcilable: Literal[True]
    fixture: bool = Field(strict=True)
    proof_hash: Hash


class ModelInput(Contract):
    instructions: str
    context: dict


class DriverCompletion(Contract):
    model: Name
    reasoning_effort: Name
    status: Literal['completed','failed','cancelled','unknown']
    quiescent: bool = Field(strict=True)
    receipt: UsageReceipt | None
    output: dict | None
    failure_code: Literal['transport_error','model_unsupported','platform_failure'] | None


class ModelInvocations:
    def __init__(self, budget, clock, driver, *, main_actor_id, allow_fixture=False):
        if budget.records is not clock.records or budget.lease != clock.lease:
            raise Conflict('runtime budget and clock must share the same owner')
        self.budget, self.clock, self.records, self.driver = budget, clock, budget.records, driver
        self.main_actor_id, self.handles = main_actor_id, {}
        try:
            self.capability = DriverCapability.model_validate(driver.capability)
        except (AttributeError, ValueError, TypeError):
            raise CostUnavailable('cost_capability_missing') from None
        cap = self.capability
        if any(not callable(getattr(driver,name,None)) for name in ('quote','start','poll','cancel','reconcile')):
            raise CostUnavailable('cost_capability_missing')
        if cap.fixture and not allow_fixture:
            raise CostUnavailable('fixture_driver_not_accepted')
        if (cap.model != budget.spec.model.model or cap.reasoning_effort != budget.spec.model.reasoning_effort
                or cap.price_table_hash != budget._state()['value']['price_table_hash']):
            raise CostUnavailable('model_or_cost_capability_mismatch')
        claim = cap.model_dump(mode='json')
        claim.pop('proof_hash')
        if self.records.artifacts.read_json(cap.proof_hash) != claim:
            raise CostUnavailable('cost_capability_missing')
        links = [l for l in self.records.related(budget.lease.test_id) if l['relation']=='candidate']
        if len(links)!=1:
            raise Conflict('runtime requires one pinned candidate')
        self.package_hash = self.records.read(links[0]['target_id'])['value']['proposal']['package_hash']
        read_candidate(self.package_hash,artifacts=self.records.artifacts)

    def _state(self):
        state = self.records.projection(self.budget.lease.test_id,'model_invocations')
        if state is None:
            return {'sequence':None,'value':{'specification_hash':self.budget.sealed.specification_hash,
                'package_hash':self.package_hash,'calls':{}}}
        if (state['value']['specification_hash'] != self.budget.sealed.specification_hash
                or state['value']['package_hash'] != self.package_hash):
            raise Conflict('runtime conditions changed')
        return state

    def _session(self, session):
        if session.records is not self.records or session.scope.test_id != self.budget.lease.test_id:
            raise Fenced('runtime session belongs to another Test')
        if not session.is_child and session.actor_id != self.main_actor_id:
            raise Fenced('runtime has one designated main actor')
        session.check()
        if self.clock._state()['value']['research_stopped']:
            raise Fenced('research has stopped')

    def _limits(self, session, state, invocation_id, logical_id, attempt):
        active = [c for key,c in state['calls'].items() if key != invocation_id and not c['quiescent']]
        if any(c['phase_id']==session.scope.phase_id and c['actor_id']==session.actor_id and c['is_child']==session.is_child for c in active):
            raise CostUnavailable('actor_already_has_active_model_call')
        children = {(c['phase_id'],c['actor_id']) for c in state['calls'].values() if c['is_child']}
        settings = self.budget.spec.runtime
        if session.is_child:
            identity = (session.scope.phase_id,session.actor_id)
            if identity not in children and settings.total_subagents is not None and len(children)>=settings.total_subagents:
                raise CostUnavailable('subagent_limit_reached')
            active_children = {(c['phase_id'],c['actor_id']) for c in active if c['is_child']}
            if identity not in active_children and len(active_children)>=settings.subagent_concurrency:
                raise CostUnavailable('child_concurrency_limit')
        if attempt==0 and logical_id not in {c['logical_id'] for c in state['calls'].values()}:
            steps = len({c['logical_id'] for c in state['calls'].values()})
            if settings.max_steps is not None and steps>=settings.max_steps:
                raise CostUnavailable('step_limit_reached')

    def _commit(self, state, invocation_id, operation, *, active_guard=None, refs=()):
        call = state['value']['calls'][invocation_id]
        action_id = invocation_id+':'+operation
        prepared = self.records.prepare(experiment_id=self.budget.experiment_id,kind='invocation',
            record_id=record_identity(self.budget.experiment_id,'model-attempt',[invocation_id,operation]),
            submission_identity=action_id,value={'invocation_id':invocation_id,'operation':operation,'call':call},
            links=(('test',self.budget.lease.test_id),),artifact_hashes=refs)
        def guard():
            if active_guard: active_guard()
            self.records._insert(prepared)
        return self.records.commit(self.budget.lease,phase_id=call['phase_id'],action_id=action_id,attempt=call['attempt'],
            kind='model_runtime',payload={'invocation_id':invocation_id,'operation':operation,'status':call['status'],
                'output_hash':call.get('output_hash'),'code':call.get('code')},simulated_at=None,
            updates=(ProjectionUpdate('model_invocations',state['sequence'],state['value']),),guard=guard)

    def begin(self, session, call_id, model_input, *, attempt=0):
        self._session(session)
        call_id = TypeAdapter(Name).validate_python(call_id)
        model_input = json.loads(encode(ModelInput.model_validate(model_input)))
        if type(attempt) is not int or not 0<=attempt<=self.budget.spec.runtime.retries:
            raise Conflict('model attempt exceeds the sealed retry policy')
        # Include Test and actor role so a child called main cannot alias its parent.
        logical_id = digest([session.scope.test_id,session.scope.phase_id,session.actor_id,session.is_child,call_id])
        invocation_id = 'model-'+digest([logical_id,attempt])
        state = self._state()
        previous = state['value']['calls'].get(invocation_id)
        if previous:
            if previous['input_hash'] != digest(model_input):
                raise Conflict('model call identity has different input')
            if previous['status'] != 'prepared':
                return self._public(session,previous,invocation_id)
        else:
            earlier = [c for c in state['value']['calls'].values() if c['logical_id']==logical_id]
            if attempt and (len(earlier)!=attempt or max(c['attempt'] for c in earlier)!=attempt-1
                            or any(c['input_hash']!=digest(model_input) for c in earlier)
                            or max(earlier,key=lambda c:c['attempt'])['status']!='failed'
                            or max(earlier,key=lambda c:c['attempt']).get('code')!='transport_error'):
                raise Conflict('retry requires the previous settled transport failure')
            self._limits(session,state['value'],invocation_id,logical_id,attempt)
            deadline = min(self.records._now()+timedelta(seconds=self.budget.spec.runtime.timeout_seconds),
                           datetime.fromisoformat(self.clock._state()['value']['deadline_at']))
            request = {'deadline_at':deadline.isoformat(),'timeout_seconds':self.budget.spec.runtime.timeout_seconds,
                       'cancel_wait_seconds':self.budget.spec.runtime.cancel_wait_seconds,'invocation_id':invocation_id,'input':model_input,'model':self.capability.model,
                'reasoning_effort':self.capability.reasoning_effort,'package_hash':self.package_hash,
                'actor_id':session.actor_id,'phase_id':session.scope.phase_id,'tools':'host_actions_only',
                'output_schema':LAgentAction.model_json_schema()}
            # quote is host-only and must not execute/bill a model call.
            try:
                bound = CallBound.model_validate(self.driver.quote(request))
            except Exception:
                raise CostUnavailable('driver_quote_failed') from None
            cap = self.capability
            expected = {'invocation_id':invocation_id,'actor_id':session.actor_id,'phase_id':session.scope.phase_id,
                'bucket':'research','resource':cap.model,'adapter_ref':cap.adapter_ref,'executor_ref':cap.executor_ref,
                'price_table_hash':cap.price_table_hash,'usage_semantics_hash':cap.usage_semantics_hash}
            if any(getattr(bound,k)!=v for k,v in expected.items()):
                raise CostUnavailable('driver_quote_binding_mismatch')
            request_hash = self.records.artifacts.put_json(request).content_hash
            call = {'logical_id':logical_id,'attempt':attempt,'input_hash':digest(model_input),'request_hash':request_hash,
                'bound':bound.model_dump(mode='json'),'actor_id':session.actor_id,'is_child':session.is_child,
                'phase_id':session.scope.phase_id,'status':'prepared','quiescent':True,'cancel_requested':False,
                'deadline_at':deadline.isoformat(),'action_hash':None,'code':None}
            state['value']['calls'][invocation_id] = call
            self._commit(state,invocation_id,'prepared',active_guard=lambda:self._session(session),
                         refs=(request_hash,bound.proof_hash,cap.proof_hash))
        state = self._state()
        call = state['value']['calls'][invocation_id]
        reserved = self.budget.reserve(call['bound'],action_id=invocation_id+':reserve',guard=lambda:self._session(session))
        if reserved['value']['payload']['response']['status']!='reserved':
            call.update(status='denied',code=reserved['value']['payload']['response']['code'],quiescent=True)
            self._commit(state,invocation_id,'denied')
            return self._public(session,call,invocation_id)
        # The budget start and runtime dispatch claim are one fenced CAS event.
        # A unique claim makes concurrent callers conflict, instead of both
        # interpreting an idempotent start acknowledgement as dispatch permission.
        state = self._state()
        call = state['value']['calls'][invocation_id]
        call.update(status='started',quiescent=False,dispatch_claim=uuid4().hex)
        def guard():
            self._session(session)
            if self.records._now()>=datetime.fromisoformat(call['deadline_at']):
                raise Fenced('model call deadline expired before dispatch')
            self._limits(session,self._state()['value'],invocation_id,logical_id,attempt)
        self.budget.start(invocation_id,action_id=invocation_id+':start',guard=guard,
            extra_updates=(ProjectionUpdate('model_invocations',state['sequence'],state['value']),))
        try:
            self._session(session)
            if self.records._now()>=datetime.fromisoformat(call['deadline_at']):
                raise Fenced('model call deadline expired before driver start')
        except Fenced:
            self.records._assert_lease(self.budget.lease)
            self.budget.mark_unknown(invocation_id,action_id=invocation_id+':suppressed-unknown')
            state = self._state()
            state['value']['calls'][invocation_id].update(status='unsettled',quiescent=True,code='dispatch_suppressed_before_start')
            self._commit(state,invocation_id,'dispatch-suppressed')
            return {'invocation_id':invocation_id,'status':'unsettled','code':'dispatch_suppressed_before_start','formal_ready':False}
        try:
            self.handles[invocation_id] = self.driver.start(self.records.artifacts.read_json(call['request_hash']))
        except Exception:
            # start may already have reached the provider: do not dispatch again.
            self.budget.mark_unknown(invocation_id,action_id=invocation_id+':start-unknown')
            state = self._state()
            state['value']['calls'][invocation_id].update(status='unsettled',code='dispatch_outcome_unknown')
            self._commit(state,invocation_id,'dispatch-unknown')
        return self._public(session,self._state()['value']['calls'][invocation_id],invocation_id)

    def _public(self, session, call, invocation_id):
        self._session(session)
        if (session.scope.phase_id,session.actor_id,session.is_child)!=(call['phase_id'],call['actor_id'],call['is_child']):
            raise Fenced('call belongs to another actor or phase')
        result = {'invocation_id':invocation_id,'status':call['status'],'code':call['code'],'formal_ready':False}
        if call['status']=='completed' and call['action_hash']:
            result['action'] = self.records.artifacts.read_json(call['action_hash'])
        return result

    def poll(self, session, invocation_id):
        call = self._state()['value']['calls'][invocation_id]
        if (session.scope.test_id,session.scope.phase_id,session.actor_id,session.is_child)!=(
                self.budget.lease.test_id,call['phase_id'],call['actor_id'],call['is_child']):
            raise Fenced('call belongs to another actor or phase')
        if call['status'] in ('completed','failed','discarded','cancelled','denied'):
            return self._public(session,call,invocation_id)
        try:
            self._session(session)
            expired = self.records._now()>=datetime.fromisoformat(call['deadline_at'])
        except Fenced:
            expired = True
        if expired: self.cancel(invocation_id)
        return self._poll(invocation_id,session)

    def reconcile(self, invocation_id):
        """Host recovery/late billing only; never releases an action to a new phase."""
        return self._poll(invocation_id,None)

    def _poll(self, invocation_id, session):
        self.records._assert_lease(self.budget.lease)
        try:
            completion = (self.driver.poll(self.handles[invocation_id]) if invocation_id in self.handles
                          else self.driver.reconcile(invocation_id))
        except Exception:
            return {'invocation_id':invocation_id,'status':'pending','code':'driver_poll_failed','formal_ready':False}
        if completion is None:
            call = self._state()['value']['calls'][invocation_id]
            timeout = call['cancel_requested'] and self.records._now() >= datetime.fromisoformat(call['cancel_requested_at']) + timedelta(seconds=self.budget.spec.runtime.cancel_wait_seconds)
            return {'invocation_id':invocation_id,'status':'pending','code':'process_cleanup_timeout' if timeout else None,'formal_ready':False}
        try:
            completion = DriverCompletion.model_validate(completion)
        except (TypeError,ValueError):
            return {'invocation_id':invocation_id,'status':'pending','code':'driver_response_invalid','formal_ready':False}
        return self._ingest(invocation_id,completion,session)

    def _ingest(self, invocation_id, completion, session):
        state = self._state()
        call = state['value']['calls'][invocation_id]
        if not completion.quiescent:
            return {'invocation_id':invocation_id,'status':'pending','code':'processes_still_active','formal_ready':False}
        identity_ok = (completion.model==self.capability.model and completion.reasoning_effort==self.capability.reasoning_effort)
        receipt = completion.receipt
        call.update(actual_model=completion.model,actual_reasoning_effort=completion.reasoning_effort)
        billed = None
        if identity_ok and receipt is not None and receipt.outcome==completion.status:
            try:
                billed = self.budget.settle(invocation_id,receipt,action_id=invocation_id+':settle')
            except Fenced:
                raise
            except (ValueError,LookupError):
                pass
        if billed is None:
            ledger = self.budget._state()['value']['invocations'][invocation_id]
            if ledger['status']=='started': self.budget.mark_unknown(invocation_id,action_id=invocation_id+':unknown')
            if call['status']=='unsettled' and call['quiescent']:
                return {'invocation_id':invocation_id,'status':'unsettled','code':call['code'],'formal_ready':False}
            call.update(status='unsettled',quiescent=True,code='usage_unsettled' if identity_ok else 'model_identity_mismatch')
            self._commit(state,invocation_id,'unsettled')
            self.handles.pop(invocation_id,None)
            return {'invocation_id':invocation_id,'status':'unsettled','code':call['code'],'formal_ready':False}
        # Billing survives phase cancellation. Action publication requires a live
        # scope again inside the final transaction, after potentially slow billing.
        action, code = None, completion.failure_code
        overrun = billed['value']['payload']['response']['upper_bound_exceeded']
        if overrun:
            code = 'cost_upper_bound_exceeded'
        if completion.status=='completed' and not overrun:
            try:
                action = LAgentAction.model_validate(completion.output).model_dump(mode='json')
            except (TypeError,ValueError):
                code = 'candidate_schema_invalid'
        def active():
            if session is None or call['cancel_requested'] or self.records._now()>=datetime.fromisoformat(call['deadline_at']):
                raise Fenced('model result is late')
            self._session(session)
        try: active()
        except Fenced:
            action, code = None, 'late_result_discarded'
        action_hash = self.records.artifacts.put_json(action).content_hash if action is not None else None
        call.update(status='completed' if action is not None else 'discarded' if code=='late_result_discarded'
                    else 'cancelled' if completion.status=='cancelled' else 'failed',
                    quiescent=True,action_hash=action_hash,output_hash=digest(completion.output),code=code)
        try:
            self._commit(state,invocation_id,'result',active_guard=active if action is not None else None,
                         refs=(action_hash,) if action_hash else ())
        except Fenced:
            # A closing race after billing must never publish an action.
            self.records._assert_lease(self.budget.lease)
            state = self._state()
            state['value']['calls'][invocation_id].update(status='discarded',quiescent=True,action_hash=None,
                output_hash=digest(completion.output),code='late_result_discarded')
            self._commit(state,invocation_id,'discarded')
        self.handles.pop(invocation_id,None)
        final = self._state()['value']['calls'][invocation_id]
        if final['status']=='completed': return self._public(session,final,invocation_id)
        return {'invocation_id':invocation_id,'status':final['status'],'code':final['code'],'formal_ready':False}

    def cancel(self, invocation_id):
        state = self._state()
        call = state['value']['calls'][invocation_id]
        if call['quiescent'] and call['status']!='prepared': return
        if call['status']=='prepared':
            ledger = self.budget._state()['value']['invocations'].get(invocation_id)
            if ledger and ledger['status']=='reserved': self.budget.release_unstarted(invocation_id,action_id=invocation_id+':release')
            call.update(status='cancelled',quiescent=True,code='cancelled_before_dispatch')
            self._commit(state,invocation_id,'cancelled-unstarted')
            return
        if not call['cancel_requested']:
            call['cancel_requested'] = True
            call['cancel_requested_at'] = self.records._now().isoformat()
            self._commit(state,invocation_id,'cancel-requested')
        # Repeated cancellation is idempotent in the driver, including after a
        # worker restart where the opaque local handle no longer exists.
        try:
            self.driver.cancel(invocation_id)
        except Exception:
            raise CostUnavailable('driver_cancel_failed') from None

    def stop_phase(self, phase_id):
        pending = []
        for invocation_id,call in self._state()['value']['calls'].items():
            if call['phase_id']!=phase_id or call['quiescent'] and call['status']!='prepared': continue
            self.cancel(invocation_id)
            if self._state()['value']['calls'][invocation_id]['status']!='cancelled':
                self._poll(invocation_id,None)
            if not self._state()['value']['calls'][invocation_id]['quiescent']: pending.append(invocation_id)
        return {'status':'stopped' if not pending else 'stopping','pending_invocations':pending,'formal_ready':False}
