import threading
import time
import unittest
from unittest.mock import Mock

import test_model_broker as fixture
from broker_pump import BrokerPump, MailboxScope
from core import object_hash
from provider_http import DeepSeekHTTP, NoRedirect, retry_delay


class Inbox:
    def __init__(self,generation):
        self.requests=[{'id':'a'*32,'generation':generation,'sha256':object_hash(fixture.BODY),'body':fixture.BODY}]
        self.responses=[]
    def pending(self):return self.requests
    def complete(self,request,**result):
        self.responses.append(result);self.requests.remove(request)


class PumpTests(unittest.TestCase):
    setUp=fixture.BrokerTests.setUp
    tearDown=fixture.BrokerTests.tearDown

    def scope(self):
        inbox=Inbox(self.phase['generation'])
        return MailboxScope(self.job,self.phase['token'],self.phase['generation'],inbox,fixture.POLICY)

    def wait_done(self,pump):
        deadline=time.monotonic()+3
        while any(not x[0].done() for x in pump.active.values()) and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(all(x[0].done() for x in pump.active.values()))

    def test_async_dispatch_uses_private_ledger_and_close_settles(self):
        gate=threading.Event();calls=[];scope=self.scope()
        def execute(body):calls.append(body);gate.wait(3);return {'kind':'response','body':fixture.response()}
        pump=BrokerPump(self.broker,execute)
        try:
            pump.tick([scope]);pump.tick([scope])
            self.assertEqual(len(pump.active),1)
            self.assertGreater(self.store.budgets.status(self.job)['reserved'],0)
            gate.set();pump.close()
            self.assertEqual(len(calls),1);self.assertEqual(len(scope.inbox.responses),1)
            self.assertEqual(self.store.budgets.status(self.job)['spent'],3404)
        finally:gate.set();pump.close()

    def test_cancelled_generation_gets_no_delivery_but_is_charged(self):
        gate=threading.Event();scope=self.scope()
        pump=BrokerPump(self.broker,lambda _: (gate.wait(3) and {'kind':'response','body':fixture.response()}))
        try:
            pump.tick([scope]);self.store.control(self.batch,'cancel');gate.set();pump.close()
            self.assertEqual(scope.inbox.responses,[])
            self.assertEqual(self.store.budgets.status(self.job)['spent'],3404)
        finally:gate.set();pump.close()

    def test_pause_keeps_existing_charge_and_blocks_new_dispatch(self):
        scope=self.scope();pump=BrokerPump(self.broker,lambda _: {'kind':'response','body':fixture.response()})
        try:
            self.store.control(self.batch,'pause');pump.tick([scope]);self.assertEqual(len(pump.active),0)
            self.store.control(self.batch,'resume');pump.tick([scope]);pump.close()
            self.assertEqual(len(scope.inbox.responses),1)
        finally:pump.close()

    def test_transport_failure_retains_request_and_reservation_during_backoff(self):
        scope=self.scope();calls=[]
        def execute(_):calls.append(1);return {'kind':'unknown','reason':'transport','retryable':True}
        pump=BrokerPump(self.broker,execute)
        try:
            pump.tick([scope]);self.wait_done(pump);pump.tick([scope])
            self.assertEqual(len(calls),1);self.assertEqual(scope.inbox.responses,[])
            self.assertGreater(self.store.budgets.status(self.job)['unknown'],0)
        finally:pump.close()

    def test_executor_failure_is_sanitized_and_retains_hold(self):
        scope=self.scope()
        def execute(_):raise RuntimeError('secret-token-must-not-escape')
        pump=BrokerPump(self.broker,execute)
        try:
            pump.tick([scope]);pump.close()
            self.assertEqual(scope.inbox.responses,[{'body':None,'error':'rejected'}])
            self.assertGreater(self.store.budgets.status(self.job)['unknown'],0)
        finally:pump.close()


class HTTPBoundaryTests(unittest.TestCase):
    def test_one_fixed_request_without_sdk_retry_or_redirect(self):
        client=DeepSeekHTTP('fixture-credential-never-used-on-network')
        result=Mock();result.status=200;result.read.return_value=b'{"id":"fixture"}'
        manager=Mock();manager.__enter__=Mock(return_value=result);manager.__exit__=Mock(return_value=False)
        client.opener=Mock();client.opener.open.return_value=manager
        self.assertEqual(client({'messages':[]})['kind'],'response')
        args=client.opener.open.call_args
        self.assertEqual(args.args[0].full_url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(client.opener.open.call_count,1)
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_proxy_scope_and_retry_after_bounds(self):
        for value in ('https://example.com','http://10.0.0.1:80','http://user:pass@127.0.0.1:7890','http://127.0.0.1:7890/path'):
            with self.assertRaises(ValueError):DeepSeekHTTP('fixture-credential',proxy=value)
        self.assertEqual(retry_delay('9000'),3600);self.assertEqual(retry_delay('-1'),0)
        self.assertEqual(retry_delay('invalid'),0)


if __name__=='__main__':unittest.main()
