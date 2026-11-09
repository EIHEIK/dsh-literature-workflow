import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from batches import BatchStore
from core import digest,atomic,canonical
from issue_packages import create_package
from test_workflow import pdf


class IssuePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output')
        self.root=Path(self.tmp.name);source=self.root/'source.pdf';pdf(source)
        self.store=BatchStore(self.root/'data');summary=self.store.prepare([{'source':str(source)}],{'revision':'fixture'})
        self.store.confirm(summary['batch'],summary['summaryHash'],'fixture');self.store.start(summary['batch'],summary['summaryHash'])
        phase=self.store.claim_next();self.job=phase['job'];self.store.fail(self.job,phase['token'],'fixture','synthetic failure')
        private=self.store.root/'broker-private';private.mkdir();(private/'secret.json').write_text('DO_NOT_BUNDLE_PRIVATE_CREDENTIAL')

    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def test_package_is_self_contained_idempotent_and_excludes_private_requests(self):
        first=create_package(self.store,self.store.phase(self.job));second=create_package(self.store,self.store.phase(self.job))
        self.assertEqual(first,second)
        self.assertEqual(self.store.status(self.store.phase(self.job)['batch'])['items'][0]['problemPackage'],first)
        with zipfile.ZipFile(first['path']) as bundle:
            self.assertEqual(set(bundle.namelist()),{'source.pdf','issue.json','README.md'})
            report=json.loads(bundle.read('issue.json'));self.assertFalse(report['acceptanceGranted'])
            self.assertEqual(report['files']['source.pdf']['sha256'],self.store.get(self.job)['sourceSha256'])
            self.assertNotIn(b'DO_NOT_BUNDLE_PRIVATE_CREDENTIAL',b''.join(bundle.read(n) for n in bundle.namelist()))

    def test_changed_source_is_reported_and_not_blessed(self):
        Path(self.store.get(self.job)['source']).write_bytes(b'changed')
        receipt=create_package(self.store,self.store.phase(self.job))
        with zipfile.ZipFile(receipt['path']) as bundle:
            self.assertNotIn('source.pdf',bundle.namelist())
            self.assertEqual(json.loads(bundle.read('issue.json'))['missingOrChanged'],['source.pdf'])

    def test_current_partial_review_text_is_kept_without_private_request_body(self):
        job=self.store.get(self.job);job['candidate']={'id':'candidate-fixture','files':{}}
        with self.store.transaction():self.store.save(job)
        private=self.store.root/'broker-private';request=private/'request.json';response=private/'response.json'
        context={'candidateId':'candidate-fixture','sourceSha256':job['sourceSha256'],'unit':{'id':'pair-0001'}}
        atomic(request,{'privateMarker':'REQUEST-MUST-NOT-BE-BUNDLED','messages':[{'role':'system','content':'test'},
            {'role':'user','content':canonical(context)}]})
        atomic(response,{'choices':[{'message':{'content':'{"verdict":"needs_attention","detail":"SYNTHETIC NEGATIVE"}'}}]})
        call={'id':'model-'+'a'*32,'job':self.job,'stage':'review','state':'completed',
              'request':{'path':str(request),'sha256':digest(request)},'response':{'path':str(response),'sha256':digest(response)}}
        self.store.db.execute('CREATE TABLE model_calls(id TEXT,job_id TEXT,body TEXT)')
        self.store.db.execute('INSERT INTO model_calls VALUES(?,?,?)',(call['id'],self.job,canonical(call)));self.store.db.commit()
        receipt=create_package(self.store,self.store.phase(self.job))
        with zipfile.ZipFile(receipt['path']) as bundle:
            name='review-units/'+call['id']+'.json';self.assertIn(name,bundle.namelist())
            text=bundle.read(name);self.assertIn(b'SYNTHETIC NEGATIVE',text)
            self.assertNotIn(b'REQUEST-MUST-NOT-BE-BUNDLED',b''.join(bundle.read(n) for n in bundle.namelist()))
            self.assertIn(b'not_accepted',text)


if __name__=='__main__':unittest.main()
