import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic, object_hash
from api_mailbox import MailboxClient, MailboxInbox


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output')
        self.root=Path(self.tmp.name);self.credential='fixture-only-'+'x'*32
        self.inbox=MailboxInbox(self.root,self.credential,1)
    def tearDown(self):self.tmp.cleanup()
    def request(self,credential=None,generation=1):
        body={'messages':[{'role':'user','content':'synthetic fixture'}]}
        request={'version':1,'id':'a'*32,'generation':generation,'credential':credential or self.credential,
                 'sha256':object_hash(body),'body':body}
        atomic(self.root/(request['id']+'.request.json'),request);return request
    def test_exchange_works_without_a_network_service_and_binds_response(self):
        client=MailboxClient(self.root,self.credential,1,timeout=3)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(client.exchange,{'messages':[{'role':'user','content':'fixture'}]})
            deadline=time.monotonic()+2;pending=[]
            while time.monotonic()<deadline and not pending:
                pending=self.inbox.pending();time.sleep(.01)
            self.assertEqual(len(pending),1)
            self.inbox.complete(pending[0],body={'choices':[{'message':{'content':'模拟译文'},'finish_reason':'stop'}]})
            self.assertEqual(future.result()['choices'][0]['message']['content'],'模拟译文')
            self.assertEqual(self.inbox.pending(),[])
    def test_wrong_credential_generation_and_body_tamper_rejected(self):
        self.request(credential='wrong')
        with self.assertRaises(ValueError):self.inbox.pending()
        self.request(generation=2)
        with self.assertRaises(ValueError):self.inbox.pending()
        request=self.request();request['body']['extra']='changed'
        atomic(self.root/(request['id']+'.request.json'),request)
        with self.assertRaises(ValueError):self.inbox.pending()
    def test_timeout_retains_request_for_unknown_usage_reconciliation(self):
        client=MailboxClient(self.root,self.credential,1,timeout=.05)
        with self.assertRaises(TimeoutError):client.exchange({'messages':[]})
        self.assertEqual(len(list(self.root.glob('*.request.json'))),1)
    def test_mismatched_old_response_refused(self):
        request=self.request()
        atomic(self.root/(request['id']+'.response.json'),{'requestSha256':'wrong','generation':1})
        with self.assertRaises(ValueError):self.inbox.pending()
    def test_upstream_errors_are_not_reflected_to_worker(self):
        self.request();pending=self.inbox.pending()[0]
        self.inbox.complete(pending,error='secret-token-in-upstream-error')
        raw=next(self.root.glob('*.response.json')).read_text(encoding='utf-8')
        self.assertNotIn('secret-token',raw)


if __name__=='__main__':unittest.main()
