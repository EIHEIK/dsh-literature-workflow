from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import urllib.error

from test_review_protocol import ReviewFixture
from core import atomic,digest,read_json
from delivery_runner import DeliveryRunner
from zotero import scope_title


class NoWorkers:
    def bind(self,store):pass
    def storage_ready(self):return True
    def mailbox_scopes(self,store):return []


class FixtureZotero:
    def __init__(self,root,source):
        self.root=root;self.source=source;self.items=[];self.paths={};self.imports=0;self.lose_second_response=False;self.parent='ABCDEFGH'
    def get(self,path):
        if path.endswith('IJKLMNOP'):return {'data':{'itemType':'attachment','parentItem':self.parent}}
        return {'data':{'itemType':'journalArticle'}}
    def file(self,key):return str(self.source) if key=='IJKLMNOP' else self.paths[key]
    def children(self,parent):return self.items
    def connect(self):pass
    def import_pdf(self,parent,path,title):
        self.imports+=1;key=f'FILE000{self.imports}';target=self.root/(key+'.pdf');shutil.copyfile(path,target)
        self.paths[key]=str(target);self.items.append({'key':key,'data':{'itemType':'attachment','parentItem':parent,'title':title}})
        if self.lose_second_response and self.imports==2:raise urllib.error.URLError('synthetic lost response after commit')


class DeliveryTests(ReviewFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        qa=Path(self.candidate['files']['qa']['path']);value=read_json(qa);value['profile']='strict-generated-cn-v1';atomic(qa,value)
        self.candidate['files']['qa']['sha256']=digest(qa)
        with self.store.transaction():
            job=self.store.get(self.job);job['candidate']=self.candidate
            job['zotero']={'libraryType':'user','parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP'};self.store.save(job)
        self.submit()
        self.fake=FixtureZotero(self.root,Path(job['source']))
        self.runner=DeliveryRunner(NoWorkers(),zotero_factory=lambda:self.fake);self.runner.bind(self.store)

    def phase_for(self,stage):
        phase={'job':self.job,'stage':stage,'generation':{'accept':5,'publish':6,'archive':7}[stage]}
        phase['worker']=self.runner.identity(phase);return phase

    def execute(self,stage):
        phase=self.phase_for(stage);self.runner.start(phase,self.store.get(self.job));return self.runner.inspect(phase)

    def published(self):
        self.assertEqual(self.execute('accept')['state'],'completed')
        with patch('core.pdf_info',side_effect=AssertionError('Host must not parse PDFs')):
            self.assertEqual(self.execute('publish')['state'],'completed')

    def test_publish_and_archive_replay_do_not_duplicate(self):
        self.published();self.assertEqual(self.execute('publish')['state'],'completed')
        self.assertEqual(self.execute('archive')['state'],'completed')
        self.assertEqual(self.execute('archive')['state'],'completed')
        self.assertEqual(self.fake.imports,2);self.assertEqual(self.store.get(self.job)['state'],'archived')

    def test_lost_import_response_reconciles_without_duplicate(self):
        self.published();self.fake.lose_second_response=True
        self.assertEqual(self.execute('archive')['state'],'terminal');self.assertEqual(self.fake.imports,2)
        self.assertEqual(self.runner.inspect(self.phase_for('archive'))['state'],'completed')
        self.assertEqual(self.fake.imports,2)

    def test_parent_change_stops_before_any_import(self):
        self.published();self.fake.parent='WRONGKEY'
        self.assertEqual(self.execute('archive')['state'],'failed');self.assertEqual(self.fake.imports,0)

    def test_corrupt_publication_is_not_overwritten(self):
        self.published();job=self.store.get(self.job);path=Path(job['publication']['outputs']['cn']['path']);path.write_bytes(b'bad')
        self.assertEqual(self.execute('publish')['state'],'failed');self.assertEqual(path.read_bytes(),b'bad')

    def test_partial_title_is_explicit(self):
        self.assertEqual(scope_title({'sourcePages':[11,12,13],'sourceInfo':{'pages':20}}),' · 部分译本 p11–13')
        self.assertEqual(scope_title({'sourcePages':[1,2],'sourceInfo':{'pages':2}}),'')


if __name__=='__main__':unittest.main()
