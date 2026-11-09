from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from batches import BatchStore
from controller import Controller
from control_api import ControlAPI
from index_refresh import IndexRefresh
from pdf_scan_worker import scan
from test_controller import FixtureRunner
from test_workflow import pdf


class IndexRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output');self.root=Path(self.tmp.name)
        source=self.root/'source.pdf';pdf(source,label='aerodynamics fixture')
        class Client:
            def get(self,path):
                return {'data':{'itemType':'attachment','contentType':'application/pdf','parentItem':'ABCDEFGH'}} if path.endswith('IJKLMNOP') else {'data':{'itemType':'journalArticle','title':'fixture'}}
            def file(self,key):return str(source)
        class Resolver:
            client=Client()
            def inventory(self,scope):return [{'parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP','title':'fixture'}],[]
        class Scanner:
            root=self.root/'scan'
            calls=0
            entered=threading.Event();release=threading.Event()
            def scan(self,path):
                self.calls+=1;self.entered.set()
                if not self.release.wait(5):raise TimeoutError('fixture timed out')
                return scan(path),{'firstIndexedAt':1}
        self.resolver=Resolver();self.scanner=Scanner();self.manager=None;self.thread=None
        self.controller=Controller(self.root/'data',FixtureRunner(),poll_seconds=.02,min_free_bytes=0,startup_hook=IndexRefresh.initialize)
        self.scope={'kind':'items','keys':['ABCDEFGH']}

    def tearDown(self):
        self.scanner.release.set()
        if self.manager:self.manager.close()
        self.controller.stop()
        if self.thread:self.thread.join(5)
        if self.manager and self.manager.thread:self.manager.thread.join(5)
        self.tmp.cleanup()

    def start(self):
        self.thread=threading.Thread(target=self.controller.run);self.thread.start();self.assertTrue(self.controller.ready.wait(5))
        self.manager=IndexRefresh(self.controller,self.resolver,self.scanner)
        self.api=ControlAPI(self.resolver,{},index_refresh=self.manager)

    def call(self,method,args):return self.controller.submit(lambda store:self.api.call(store,method,args))

    def await_terminal(self,index):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.call('get_index_status',{'index':index})
            if result['state'] in ('completed','completed_with_issues','blocked'):return result
            time.sleep(.02)
        self.fail('Index did not finish')

    def test_async_index_keeps_api_responsive_and_ack_replay_deduplicates(self):
        self.start();args={'scope':self.scope,'requestId':'fixture-request-0001'}
        first=self.call('refresh_index',args);self.manager.start()
        self.assertTrue(self.scanner.entered.wait(3))
        self.assertFalse(self.call('get_capabilities',{})['productionReady'])
        replay=self.call('refresh_index',args);self.assertEqual(first['index'],replay['index'])
        with self.assertRaises(ValueError):self.call('refresh_index',{**args,'scope':{'kind':'items','keys':['ZYXWVUTS']}})
        self.scanner.release.set();result=self.await_terminal(first['index'])
        self.assertEqual(result['state'],'completed');self.assertEqual(self.scanner.calls,1)
        counts=self.controller.submit(lambda store:{name:store.db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('jobs','batches','corpus_sources','corpus_trust')})
        self.assertEqual(counts,{'jobs':0,'batches':0,'corpus_sources':1,'corpus_trust':0})

    def test_restart_continues_persisted_inventory_without_reinventing_scope(self):
        # Seed an interrupted inventory in one closed store, then reopen via the
        # real controller. This fixture tests durable queue resumption, while
        # Docker worker reattachment has its separate real-container evidence.
        store=BatchStore(self.root/'data');IndexRefresh.initialize(store)
        manager=IndexRefresh(self.controller,self.resolver,self.scanner)
        run=manager.request(store,self.scope,'fixture-restart-0001')
        run['state']='running';run['inventory']=[{'parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP','title':'fixture','state':'running'}]
        with store.transaction():manager.save(store,run)
        store.db.close();self.start()
        self.resolver.inventory=lambda scope:(_ for _ in ()).throw(AssertionError('Must use frozen inventory'))
        self.scanner.release.set();self.manager.start()
        self.assertEqual(self.await_terminal(run['index'])['state'],'completed')

    def test_failed_source_is_visible_and_does_not_spend_or_publish(self):
        self.start();self.scanner.scan=lambda path:(_ for _ in ()).throw(ValueError('bad PDF'))
        first=self.call('refresh_index',{'scope':self.scope,'requestId':'fixture-failure-0001'});self.manager.start()
        result=self.await_terminal(first['index'])
        self.assertEqual(result['state'],'completed_with_issues');self.assertEqual(result['inventory'][0]['state'],'failed')
        self.assertEqual(result['modelCalls'],0)

    def test_scope_and_request_id_reject_model_paths_and_commands(self):
        self.start()
        for args in ({'scope':self.scope,'requestId':'short'},
                     {'scope':{**self.scope,'path':'C:/private'},'requestId':'fixture-invalid-0001'},
                     {'scope':self.scope,'requestId':'fixture-invalid-0001','command':'anything'}):
            with self.assertRaises(ValueError):self.call('refresh_index',args)


if __name__=='__main__':unittest.main()
