from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from batches import BatchStore
from core import digest
from exceptions import ExceptionStore, https_target
from test_workflow import pdf


class ExceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output')
        self.root=Path(self.tmp.name); src=self.root/'source.pdf'; pdf(src)
        self.store=BatchStore(self.root/'data'); summary=self.store.prepare([{'source':str(src)}],{})
        self.batch=summary['batch']; self.job=summary['items'][0]['job']
        self.store.confirm(self.batch,summary['summaryHash'],'fixture-user')
        self.store.start(self.batch,summary['summaryHash'])
        self.ex=ExceptionStore(self.store,[self.root/'data'])
    def tearDown(self): self.store.db.close();self.tmp.cleanup()
    def approved(self,resource=None):
        request=self.ex.request(self.job,'https_domain',resource or {'host':'example.org'},'fixture source lookup')
        self.ex.review(request['id'],'allow','relevant public reference','fixture-host')
        return request['id']
    def test_grants_are_one_use_exact_resource_and_job_bound(self):
        rid=self.approved()
        with self.assertRaises(ValueError):self.ex.consume(rid,self.job,{'host':'other.org'},lambda _:True)
        self.ex.consume(rid,self.job,{'host':'example.org'},lambda _:True)
        with self.assertRaises(ValueError):self.ex.consume(rid,self.job,{'host':'example.org'},lambda _:True)
    def test_approval_never_replaces_execution_time_validation(self):
        rid=self.approved()
        with self.assertRaisesRegex(ValueError,'revalidation'):self.ex.consume(rid,self.job,{'host':'example.org'},lambda _:False)
        self.assertEqual(self.ex.get(rid)['state'],'allowed')
        self.store.control(self.batch,'pause')
        with self.assertRaisesRegex(ValueError,'not running'):self.ex.consume(rid,self.job,{'host':'example.org'},lambda _:True)
    def test_control_review_requires_transport_verified_supervisor(self):
        from control_api import ControlAPI
        api=ControlAPI(None,{})
        request=self.ex.request(self.job,'https_domain',{'host':'example.org'},'fixture lookup')
        args={'request':request['id'],'decision':'allow','reason':'public relevant source'}
        with self.assertRaises(PermissionError):api.call(self.store,'review_exception',args)
        with self.assertRaises(ValueError):api.call(self.store,'review_exception',{**args,'actor':'codex:forged'})
        result=api.call(self.store,'review_exception',args,actor='codex:fixture:turn')
        self.assertEqual(result['state'],'allowed')
        self.assertEqual(len(api.call(self.store,'get_exceptions',{'batch':self.batch})['exceptions']),1)

    def test_arbitrary_categories_wildcards_and_long_ttl_refused(self):
        for kind,resource in [('shell',{'command':'echo hi'}),('budget',{'cap':999}),('https_domain',{'host':'*.example.org'})]:
            with self.assertRaises(ValueError):self.ex.request(self.job,kind,resource,'fixture')
        with self.assertRaises(ValueError):self.ex.request(self.job,'https_domain',{'host':'example.org'},'fixture',ttl=3601)
    def test_corpus_read_cannot_escape_or_change_after_review(self):
        outside=self.root/'outside.txt';outside.write_text('private')
        with self.assertRaisesRegex(ValueError,'outside'):
            self.ex.request(self.job,'corpus_read',{'path':str(outside),'sha256':digest(outside)},'fixture')
        inside=self.store.root/'reference.txt';inside.write_text('reference')
        resource={'path':str(inside),'sha256':digest(inside)}
        rid=self.ex.request(self.job,'corpus_read',resource,'fixture')['id']
        self.ex.review(rid,'allow','relevant','fixture-host');inside.write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):self.ex.consume(rid,self.job,resource,lambda _:True)
    def test_dns_private_mixed_addresses_and_scheme_refused(self):
        def addresses(*values):
            return lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(v,443)) for v in values]
        for value in ('127.0.0.1','10.0.0.1','169.254.169.254','192.168.1.1','::1'):
            with self.assertRaises(ValueError):https_target('https://example.org',addresses(value))
        with self.assertRaises(ValueError):https_target('https://example.org',addresses('8.8.8.8','127.0.0.1'))
        for url in ('http://example.org','https://user:pass@example.org','https://example.org:444','https://example.org/#x'):
            with self.assertRaises(ValueError):https_target(url,addresses('8.8.8.8'))
        self.assertEqual(https_target('https://example.org/paper',addresses('8.8.8.8'))['host'],'example.org')


if __name__=='__main__':unittest.main()
