import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from batches import BatchStore
from budget import BudgetExceeded
from model_broker import ModelBroker, PriceBook, BrokerBlocked, BrokerWait, provider_payload
from test_workflow import pdf

ROOT=Path(__file__).resolve().parents[1]
POLICY={'model':'deepseek-flash','thinking':'disabled','maxOutputTokens':4096}
BODY={'messages':[{'role':'user','content':'Translate this fixture.'}]}

def response(**changes):
    result={'id':'offline-fixture','model':'deepseek-flash',
            'usage':{'prompt_tokens':1000,'completion_tokens':200,'total_tokens':1200,
                     'prompt_cache_hit_tokens':100,'prompt_cache_miss_tokens':900},
            'choices':[{'finish_reason':'stop','message':{'content':'离线夹具','reasoning_content':'not retained'}}]}
    result.update(changes)
    return result


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output')
        self.root=Path(self.tmp.name);source=self.root/'source.pdf';pdf(source)
        self.store=BatchStore(self.root/'data')
        self.summary=self.store.prepare([{'source':str(source),'title':'fixture'}],{'revision':'test'})
        self.batch=self.summary['batch'];self.job=self.summary['items'][0]['job']
        self.store.confirm(self.batch,self.summary['summaryHash'],'offline-fixture-confirmation')
        self.store.start(self.batch,self.summary['summaryHash']);self.phase=self.store.claim_next()
        book=json.loads((ROOT/'deploy/deepseek-pricing.json').read_text())
        book.pop('inputReservation',None) # Legacy full-context fallback remains covered.
        book.update(verifiedDate='2000-01-01',validThrough='2099-01-01')
        self.book=PriceBook(book);self.broker=ModelBroker(self.store,self.book)

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def prepare(self,body=None):
        return self.broker.prepare(self.job,self.phase['token'],body or BODY,POLICY)

    def skip_delay(self):
        self.store.db.execute("DELETE FROM broker_meta WHERE key='nextDispatchAt'");self.store.db.commit()

    def test_exact_usage_upper_bound_and_cache_do_not_rebill(self):
        call=self.prepare();held=self.store.budgets.status(self.job)['reserved']
        self.assertGreater(held,2_000_000)
        result=self.broker.finish(call['call'],response())
        self.assertEqual(result['state'],'completed')
        self.assertNotIn('reasoning_content',json.dumps(result))
        self.assertEqual(self.store.budgets.status(self.job)['spent'],3404)
        self.assertEqual(self.store.budgets.status('pilot:first')['spent'],3404)
        self.assertEqual(self.prepare()['action'],'cached')
        self.assertEqual(self.store.budgets.status(self.job)['reserved'],0)
        with self.assertRaises(ValueError):self.broker.finish(call['call'],response())

    def test_inflight_dedup_and_global_concurrency(self):
        self.prepare()
        with self.assertRaises(BrokerWait):self.prepare()
        self.skip_delay();self.prepare({'messages':[{'role':'user','content':'second'}]})
        self.skip_delay()
        with self.assertRaisesRegex(BrokerWait,'concurrency'):
            self.prepare({'messages':[{'role':'user','content':'third'}]})

    def test_known_usage_settles_even_malformed_or_truncated_content(self):
        for choices in ([None],{'bad':'shape'},[],[{'message':None}],[{'finish_reason':'length','message':{'content':'partial'}}]):
            call=self.prepare({'messages':[{'role':'user','content':repr(choices)}]})
            result=self.broker.finish(call['call'],response(choices=choices))
            self.assertEqual(result['state'],'rejected');self.skip_delay()
        self.assertEqual(self.store.budgets.status(self.job)['spent'],5*3404)
        self.assertEqual(self.store.budgets.status(self.job)['unknown'],0)

    def test_missing_usage_retains_reservation_and_blocks_silent_retry(self):
        call=self.prepare();result=self.broker.finish(call['call'],response(usage=None))
        self.assertEqual(result['state'],'unknown')
        self.assertGreater(self.store.budgets.status(self.job)['unknown'],2_000_000)
        with self.assertRaises(BrokerBlocked):self.prepare()

    def test_cancel_does_not_discard_provider_charge(self):
        call=self.prepare();self.store.control(self.batch,'cancel')
        self.broker.finish(call['call'],response())
        self.assertEqual(self.store.budgets.status(self.job)['spent'],3404)
        with self.assertRaises(ValueError):self.prepare()

    def test_unknown_retry_limit_keeps_all_attempt_holds(self):
        for attempt in range(1,5):
            call=self.prepare();self.assertEqual(self.broker.get(call['call'])['attempt'],attempt)
            self.broker.unknown(call['call'],'timeout')
            saved=self.broker.get(call['call']);saved['retryAt']=0;self.broker.save(saved)
            self.skip_delay()
        with self.assertRaisesRegex(BrokerBlocked,'retry limit'):self.prepare()
        self.assertEqual(self.store.budgets.status(self.job)['unknown'],4*self.book.reservation(4096))

    def test_restart_marks_dispatch_unknown_without_refund(self):
        call=self.prepare()
        self.assertEqual(self.broker.recover_controller_restart(),[call['call']])
        self.assertEqual(self.broker.recover_controller_restart(),[])
        self.assertGreater(self.store.budgets.status(self.job)['unknown'],0)

    def test_shared_provider_fault_pauses_dispatch_and_keeps_charge_hold(self):
        call=self.prepare()
        self.broker.unknown(call['call'],'authentication',retryable=False)
        self.assertEqual(self.store.batch(self.batch)['state'],'paused')
        self.assertGreater(self.store.budgets.status(self.job)['unknown'],0)
        self.assertIsNone(self.store.claim_next())
        with self.assertRaises(BrokerWait):self.prepare()

    def test_private_cache_tampering_is_rejected(self):
        call=self.prepare();self.broker.finish(call['call'],response())
        path=Path(self.broker.get(call['call'])['response']['path']);path.write_text('{}')
        with self.assertRaisesRegex(BrokerBlocked,'cache changed'):self.prepare()

    def test_request_policy_cannot_be_changed_by_worker(self):
        payload=provider_payload({**BODY,'model':'other','max_tokens':1,'thinking':{'type':'enabled'}},POLICY)
        self.assertEqual(payload['model'],'deepseek-flash');self.assertEqual(payload['max_tokens'],4096)
        with self.assertRaises(BrokerBlocked):provider_payload({**BODY,'tools':[]},POLICY)
        with self.assertRaises(BrokerBlocked):provider_payload({'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'https://example.com/private.png'}}]}]},POLICY)

    def test_stale_tariff_and_corrupt_usage_fail_closed(self):
        data=copy.deepcopy(self.book.data);data['validThrough']='2001-01-01'
        with self.assertRaises(BrokerBlocked):PriceBook(data).reservation(4096)
        with self.assertRaises(ValueError):self.book.settlement({'prompt_tokens':1,'completion_tokens':2,'total_tokens':4})
        with self.assertRaises(ValueError):self.book.settlement({'prompt_tokens':True,'completion_tokens':2,'total_tokens':3})

    def bounded_broker(self):
        data=copy.deepcopy(self.book.data)
        data['inputReservation']=json.loads((ROOT/'deploy/deepseek-pricing.json').read_text())['inputReservation']
        book=PriceBook(data);self.broker=ModelBroker(self.store,book);return book

    def test_verified_input_bound_keeps_output_maximum_and_does_not_raise_budget(self):
        book=self.bounded_broker();call=self.prepare();saved=self.broker.get(call['call'])
        self.assertLess(saved['reserved'],book.reservation(4096))
        self.assertGreater(saved['reserved'],book.cost(0,0,4096))
        self.assertEqual(self.store.budgets.status(self.job)['cap'],10_000_000)
        self.broker.finish(call['call'],response())
        self.assertEqual(self.store.budgets.status(self.job)['spent'],3404)

    def test_prompt_bound_violation_keeps_bill_and_pauses_instead_of_delivering(self):
        self.bounded_broker();call=self.prepare();ceiling=self.broker.get(call['call'])['inputTokenCeiling'];n=ceiling+1
        result=self.broker.finish(call['call'],response(usage={'prompt_tokens':n,'completion_tokens':1,'total_tokens':n+1}))
        self.assertEqual(result['state'],'rejected');self.assertEqual(self.store.batch(self.batch)['state'],'paused')
        self.assertGreater(self.store.budgets.status(self.job)['spent'],0)

    def test_utf8_and_each_image_count_in_bound(self):
        book=self.bounded_broker();base={'messages':[{'role':'user','content':[{'type':'text','text':'η中文🙂'}]}]}
        plain=book.input_bound(base);base['messages'][0]['content'].append({'type':'image_url','image_url':{'url':'inline fixture'}})
        self.assertEqual(book.input_bound(base)-plain,1024)
        self.assertGreater(plain,len('η中文🙂'.encode('utf-8')))


if __name__=='__main__':unittest.main()
