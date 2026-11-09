import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import Store,atomic,read_json,digest,dispatch,CHECKS
from zotero import archive,local_url
import pymupdf as fitz

def pdf(path,pages=2,label='fixture'):
    d=fitz.open()
    for n in range(pages):
        p=d.new_page(width=240,height=300);p.insert_text((20,40),f'{label} page {n+1}: Upper(j) = 1.0')
    d.save(path);d.close()

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        test_root=Path(__file__).resolve().parents[1]/'.test-output';test_root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=test_root);self.root=Path(self.tmp.name);self.src=self.root/'source.pdf';self.mono=self.root/'mono.pdf'
        pdf(self.src);pdf(self.mono,label='translated fixture');self.s=Store(self.root/'state')
        self.j=self.call('create',source=str(self.src),zotero={'parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP'})
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def call(self,action,actor='producer',job=None,**kw):
        with self.s.transaction():return dispatch(self.s,{'action':action,'job':job or getattr(self,'j',{}).get('id'),**kw},actor)
    def stage(self,**kw):return self.call('stage',mono=str(self.mono),**kw)
    def review(self):
        t=self.call('review_begin',actor='independent-reviewer');r=read_json(t['template']);c=t['candidate']
        r.update(semanticSourcePages=self.j['sourcePages'],visualCnPages=list(range(1,c['layout']['cnPages']+1)),
                 visualBiPages=list(range(1,c['layout']['biPages']+1)),checks={k:True for k in CHECKS},verdict='pass',summary='SYNTHETIC TEST REVIEW, not a paper quality claim')
        p=self.root/'review.json';atomic(p,r);self.call('review_submit',actor='independent-reviewer',report=str(p));return p
    def accepted(self):self.stage();self.review();return self.call('accept')
    def test_create_deduplicates(self):
        j=self.call('create',source=str(self.src),zotero={'parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP'})
        self.assertEqual(j['id'],self.j['id'])
    def test_missing_source(self):
        with self.assertRaises(FileNotFoundError):self.call('create',source=str(self.root/'missing.pdf'))
    def test_deleted_tail_page_rejected(self):
        short=self.root/'short.pdf';pdf(short,1)
        with self.assertRaisesRegex(ValueError,'Missing source pages'):self.call('stage',mono=str(short),mapping=[1])
    def test_unregistered_review_rejected(self):
        self.stage();p=self.root/'fake.json';atomic(p,{'ticket':'unknown','candidateId':'nonexistent'})
        with self.assertRaisesRegex(ValueError,'Unregistered'):self.call('review_submit',actor='other',report=str(p))
    def test_producer_cannot_review(self):
        self.stage()
        with self.assertRaisesRegex(ValueError,'another real'):self.call('review_begin')
    def test_current_candidate_required_after_second_same_page_change(self):
        self.accepted()
        pdf(self.root/'b.pdf',label='changed B');self.call('stage',mono=str(self.root/'b.pdf'));self.review()
        pdf(self.root/'c.pdf',label='changed C');self.call('stage',mono=str(self.root/'c.pdf'))
        with self.assertRaisesRegex(ValueError,'dependency invalid'):self.call('accept')
    def test_empty_coverage_rejected(self):
        self.stage();t=self.call('review_begin',actor='other')
        with self.assertRaisesRegex(ValueError,'coverage'):self.call('review_submit',actor='other',report=t['template'])
    def test_changed_review_file_invalidates_acceptance(self):
        j=self.accepted();Path(j['acceptance']['reviewFile']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'dependency invalid'):self.call('publish')
    def test_changed_sidecar_rejected(self):
        j=self.stage();Path(j['candidate']['files']['layout']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'changed'):self.call('review_begin',actor='other')
    def test_changed_preview_rejected(self):
        j=self.stage();p=next(v['path'] for k,v in j['candidate']['files'].items() if k.startswith('preview/'));Path(p).write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'changed'):self.call('resume')
    def test_corrupted_published_file_never_recovered_as_good(self):
        self.accepted();j=self.call('publish');Path(j['publication']['outputs']['cn']['path']).write_bytes(b'NOT A PDF')
        with self.assertRaisesRegex(ValueError,'corrupt'):self.call('resume')
        with self.assertRaisesRegex(ValueError,'corrupt'):self.call('publish')
    def test_publish_idempotent_and_layout_revision_new_identity(self):
        self.accepted();old=self.call('publish');again=self.call('publish');self.assertEqual(old['publication'],again['publication'])
        fresh=self.call('revise',revision='layout-r2');self.assertNotEqual(fresh['id'],old['id']);self.j=fresh
        self.stage(supplements=[{'sourcePage':1,'text':'Upper layer：上层；Wind tunnel：风洞'}]);self.review();self.call('accept');new=self.call('publish')
        self.assertNotEqual(new['publication']['id'],old['publication']['id'])
        self.assertNotEqual(new['publication']['outputs']['cn']['sha256'],old['publication']['outputs']['cn']['sha256'])
    def test_cancel_rejects_late_worker(self):
        claimed=self.call('claim');self.call('cancel')
        with self.s.transaction():
            with self.assertRaisesRegex(ValueError,'stale'):self.s.stage(self.j['id'],{'mono':str(self.mono)},'producer',token=claimed['lease'])
    def test_only_one_active_paper_globally(self):
        self.call('claim');other=self.call('create',source=str(self.src),config={'revision':'other'})
        with self.assertRaisesRegex(ValueError,'one active'):self.call('claim',job=other['id'])
    def test_claim_atomic_across_processes(self):
        script=Path(__file__).resolve().parents[1]/'runtime/core.py'
        request=json.dumps({'root':str(self.s.root),'actor':'producer','args':{'action':'claim','job':self.j['id']}})
        ps=[subprocess.Popen([sys.executable,str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE) for _ in range(2)]
        for p in ps:p.stdin.write(request.encode());p.stdin.close()
        results=[json.loads(p.stdout.read()) for p in ps]
        for p in ps:p.wait();p.stdout.close()
        self.assertEqual(sum(r['ok'] for r in results),1)
    def test_cli_stdio_utf8_without_utf8_locale(self):
        """CLI must emit/accept UTF-8 even when the parent locale is cp936 (regression)."""
        script=Path(__file__).resolve().parents[1]/'runtime/core.py'
        title='中文标题·公式 Γ=0.5'
        request=json.dumps({'root':str(self.s.root),'actor':'producer','args':{'action':'create','source':str(self.src),'title':title,'config':{'revision':'utf8-locale'}}})
        env={**os.environ,'PYTHONIOENCODING':'cp936','PYTHONUTF8':'0'}
        p=subprocess.run([sys.executable,str(script)],input=request.encode('utf-8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8','replace')[-400:])
        result=json.loads(p.stdout.decode('utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['title'],title)

    def test_real_crash_after_publication_files_before_sql_commit(self):
        self.accepted()
        code="from core import Store;import os;s=Store(os.environ['TEST_ROOT']);s.db.execute('BEGIN IMMEDIATE');s.publish(os.environ['TEST_JOB']);os._exit(77)"
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'runtime'),'TEST_ROOT':str(self.s.root),'TEST_JOB':self.j['id']}
        result=subprocess.run([sys.executable,'-c',code],env=env);self.assertEqual(result.returncode,77)
        self.assertEqual(self.s.get(self.j['id'])['state'],'accepted')
        new=self.call('publish');self.assertEqual(new['state'],'published')
        self.assertEqual(digest(new['publication']['outputs']['cn']['path']),new['candidate']['files']['cn']['sha256'])
    def test_real_crash_with_corruption_before_recovery_rejected(self):
        self.accepted()
        code="from core import Store;from pathlib import Path;import os;s=Store(os.environ['TEST_ROOT']);s.db.execute('BEGIN IMMEDIATE');j=s.publish(os.environ['TEST_JOB']);Path(j['publication']['outputs']['cn']['path']).write_bytes(b'bad');os._exit(78)"
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'runtime'),'TEST_ROOT':str(self.s.root),'TEST_JOB':self.j['id']}
        p=subprocess.run([sys.executable,'-c',code],env=env);self.assertEqual(p.returncode,78)
        with self.assertRaisesRegex(ValueError,'corrupt'):self.call('publish')
    def test_continuations_and_supplements_have_explicit_mapping(self):
        p=self.root/'long.pdf';pdf(p,3);j=self.call('stage',mono=str(p),mapping=[1,1,2],supplements=[{'sourcePage':2,'text':'图内标签对照：升力系数、攻角。'}])
        self.assertEqual(j['candidate']['layout']['cnPages'],4);self.assertEqual([r['sourcePage'] for r in j['candidate']['layout']['rows']],[1,1,2,2])
    def test_archive_second_import_failure_recovers_without_duplicate(self):
        self.accepted();self.call('publish');fake=FakeZotero(self.root)
        with self.assertRaisesRegex(RuntimeError,'after remote commit'):
            with self.s.transaction():archive(self.s,self.j['id'],{},fake)
        self.assertEqual(fake.imports,2)
        with self.s.transaction():result=archive(self.s,self.j['id'],{},fake)
        self.assertEqual(result['state'],'archived');self.assertEqual(fake.imports,2)
    def test_archive_unknown_intent_does_not_duplicate(self):
        self.accepted();self.call('publish');fake=FakeZotero(self.root,fail_before=True)
        with self.assertRaises(RuntimeError):
            with self.s.transaction():archive(self.s,self.j['id'],{},fake)
        with self.assertRaisesRegex(ValueError,'reconciliation'):
            with self.s.transaction():archive(self.s,self.j['id'],{},fake)
        self.assertEqual(fake.imports,1)
    def test_remote_zotero_url_rejected(self):
        with self.assertRaises(ValueError):local_url('https://example.com/mcp')

class FakeZotero:
    def __init__(self,root,fail_before=False):self.root=root;self.items=[];self.paths={};self.imports=0;self.fail_before=fail_before
    def connect(self):return []
    def children(self,parent):return self.items
    def file(self,k):return self.paths[k]
    def import_pdf(self,parent,path,title):
        self.imports+=1
        if self.fail_before:raise RuntimeError('unknown connection failure before response')
        k=f'FILE000{self.imports}';target=self.root/(k+'.pdf');shutil.copyfile(path,target);self.paths[k]=str(target)
        self.items.append({'key':k,'data':{'itemType':'attachment','parentItem':parent,'title':title}})
        if self.imports==2:raise RuntimeError('lost response after remote commit')

if __name__=='__main__':unittest.main(verbosity=2)
