"""Nonblocking controller-side mailbox dispatch with private model receipts.

The caller supplies registered mailboxes and policies from trusted stage setup,
never from a tool payload. SQLite is only touched by tick/close on its owner.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from budget import BudgetExceeded
from model_broker import BrokerBlocked, BrokerWait


@dataclass(frozen=True)
class MailboxScope:
    job: str
    token: str
    generation: int
    inbox: object
    policy: dict


class BrokerPump:
    def __init__(self,broker,executor):
        self.broker=broker;self.executor=executor
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='literature-provider')
        self.active={};self.closed=False

    def _deliver(self,scope,request,body=None,error=None,call_id=None):
        phase=self.broker.store.phase(scope.job)
        if phase['state']!='running' or phase['token']!=scope.token or phase['generation']!=scope.generation:return False
        try:
            if body is not None:
                if call_id is None:raise ValueError('Private provider receipt required')
                self.broker.record_delivery(scope,request,call_id)
            scope.inbox.complete(request,body=body,error=error)
        except (ValueError,OSError):
            with self.broker.store.transaction():
                self.broker.store.event('mailbox.delivery_failed',phase['batch'],scope.job,request=request['id'])
            return False
        return True

    def collect(self):
        for key,entry in list(self.active.items()):
            future,call_id,scope,request=entry
            if not future.done():continue
            try: result=future.result()
            except Exception: result={'kind':'unknown','reason':'executor_error','retryable':False}
            if not isinstance(result,dict):result={'kind':'unknown','reason':'invalid_executor_response','retryable':False}
            if result.get('kind')=='response':settled=self.broker.finish(call_id,result.get('body'))
            else:
                settled=self.broker.unknown(call_id,result.get('reason','invalid_executor_response'),
                    retryable=result.get('retryable',False),retry_after=result.get('retryAfter',0))
            del self.active[key]
            if settled['state']=='completed':self._deliver(scope,request,body=settled['response'],call_id=call_id)
            elif not settled.get('retryable'):self._deliver(scope,request,error='rejected')
            # Retryable unknown: retain the same mailbox request; broker's durable
            # backoff, attempt count and cumulative reservations govern the retry.

    def tick(self,scopes):
        if self.closed:raise RuntimeError('Broker pump is closed')
        self.collect()
        for scope in scopes:
            phase=self.broker.store.phase(scope.job)
            if phase['state']!='running' or phase['token']!=scope.token or phase['generation']!=scope.generation:continue
            try:pending=scope.inbox.pending()
            except (ValueError,OSError):
                self.broker.store.fail(scope.job,scope.token,'mailbox','Scoped request transport failed validation')
                continue
            for request in pending:
                key=(scope.job,scope.generation,request['id'])
                if key in self.active:continue
                try:prepared=self.broker.prepare(scope.job,scope.token,request['body'],scope.policy)
                except BrokerWait:continue
                except BudgetExceeded:
                    # Holds for *other live* calls can clear when their usage is
                    # settled. Do not fail a paper simply because slots are full.
                    budgets=self.broker.store.budgets
                    scopes_to_check=[scope.job]+(['pilot:first'] if self.broker.store.batch(phase['batch'])['pilot'] else [])
                    if any(budgets.status(s)['reserved'] for s in scopes_to_check):continue
                    self.broker.store.fail(scope.job,scope.token,'budget','Cumulative budget cannot cover another bounded request')
                    self._deliver(scope,request,error='budget');break
                except (BrokerBlocked,ValueError,OSError):
                    self._deliver(scope,request,error='rejected');continue
                if prepared['action']=='cached':self._deliver(scope,request,body=prepared['response'],call_id=prepared['call']);continue
                try:future=self.pool.submit(self.executor,prepared['payload'])
                except Exception:
                    self.broker.unknown(prepared['call'],'executor_start',retryable=False)
                    self._deliver(scope,request,error='executor_start');continue
                self.active[key]=(future,prepared['call'],scope,request)

    def close(self):
        # Graceful shutdown settles already-dispatched requests before releasing
        # the controller's process lock. A forced process death is reconciled as
        # unknown by the next owner, never refunded as an unsent request.
        if self.closed:return
        self.pool.shutdown(wait=True,cancel_futures=False)
        self.collect();self.closed=True
