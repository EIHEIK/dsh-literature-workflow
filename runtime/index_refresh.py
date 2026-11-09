"""Durable, unpaid Zotero index jobs; SQLite is owned by the controller only.

One separate thread retrieves/extracts a single document at a time. A restarted
manager resumes its persisted inventory and PDFScanner's existing worker intent.
"""
import json
import threading
import time

from core import canonical,object_hash
from corpus import CorpusIndex
from instance_lock import InstanceLock
from library_index import extract_attachment,register_extraction


class IndexRefresh:
    def __init__(self,controller,resolver,scanner):
        self.controller=controller;self.resolver=resolver;self.scanner=scanner
        self.stopping=threading.Event();self.thread=None;self.error=None

    @staticmethod
    def initialize(store):
        CorpusIndex(store.db)
        store.db.execute('CREATE TABLE IF NOT EXISTS index_runs(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
        store.db.commit()

    @staticmethod
    def get(store,index):
        row=store.db.execute('SELECT body FROM index_runs WHERE id=?',(index,)).fetchone()
        if row is None:raise ValueError('Unknown index refresh')
        return json.loads(row[0])

    @staticmethod
    def save(store,run):
        run['updatedAt']=time.time()
        store.db.execute('INSERT OR REPLACE INTO index_runs VALUES(?,?)',(run['index'],canonical(run)))

    def request(self,store,scope,request_id):
        scope={'kind':scope['kind'],'keys':sorted(set(scope['keys']))}
        identity='index-'+object_hash({'requestId':request_id})[:32]
        row=store.db.execute('SELECT body FROM index_runs WHERE id=?',(identity,)).fetchone()
        if row:
            run=json.loads(row[0])
            if run['scope']!=scope:raise ValueError('Index request ID was reused with different scope')
            return run
        active=[json.loads(r[0]) for r in store.db.execute('SELECT body FROM index_runs')]
        active=[r for r in active if r['state'] in ('queued','running')]
        if len(active)>=8:raise ValueError('Index queue is full')
        if any(r['scope']==scope for r in active):raise ValueError('Scope already has an active refresh; inspect its existing request')
        run={'index':identity,'scope':scope,'state':'queued','createdAt':time.time(),
             'inventory':None,'omissions':[],'modelCalls':0}
        with store.transaction():
            self.save(store,run);store.event('index.queued',index=identity)
        return run

    def start(self):
        if self.thread:raise RuntimeError('Index manager already started')
        self.thread=threading.Thread(target=self.run,name='literature-index-refresh')
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:self.thread.join(timeout=1)

    def call(self,fn):
        if self.stopping.is_set():raise RuntimeError('Index manager stopping')
        return self.controller.submit(fn)

    def next_run(self,store):
        runs=[json.loads(r[0]) for r in store.db.execute('SELECT body FROM index_runs')]
        active=sorted((r for r in runs if r['state'] in ('queued','running')),key=lambda r:r['createdAt'])
        return active[0] if active else None

    def run(self):
        try:
            # A new service cannot race an old scan that is still returning.
            with InstanceLock(self.scanner.root/'manager-lock'):
                while not self.stopping.is_set() and not self.controller.stopping.is_set():
                    current=self.call(self.next_run)
                    if current is None:self.stopping.wait(.5);continue
                    self.process(current)
        except Exception as error:
            if not self.stopping.is_set() and not self.controller.stopping.is_set():self.error=type(error).__name__

    def persist(self,run):
        def operation(store):
            with store.transaction():self.save(store,run)
        self.call(operation)

    def process(self,run):
        try:
            if run['inventory'] is None:
                items,omissions=self.resolver.inventory(run['scope'])
                run.update(state='running',inventory=[{**item,'state':'queued'} for item in items],omissions=omissions)
                self.persist(run)
            for item in run['inventory']:
                if item['state']=='done':continue
                # A previous failed item is retained in this run. A new explicit
                # refresh can inspect the same scanner intent, never blind-spawn.
                if item['state']=='failed':continue
                item['state']='running';self.persist(run)
                try:
                    extracted=extract_attachment(self.resolver.client,self.scanner,item['parentItemKey'],item['sourceAttachmentKey'])
                    result=self.call(lambda store:register_extraction(store,extracted))
                    item.update(state='done',result=result)
                except (OSError,ValueError,KeyError,TimeoutError) as error:
                    item.update(state='failed',error=type(error).__name__,reason='source_or_extraction_requires_attention')
                self.persist(run)
            issues=bool(run['omissions']) or any(i['state']!='done' or i['result']['status']!='indexed' for i in run['inventory'])
            run['state']='completed_with_issues' if issues else 'completed'
            run['completedAt']=time.time();self.persist(run)
        except RuntimeError:
            # Controller shutdown or an uncertain Docker observation: retain the
            # running intent; do not invent a new worker or clear progress.
            if self.controller.stopping.is_set() or self.stopping.is_set():return
            run.update(state='blocked',reason='observation_or_controller_unavailable');self.persist(run)
        except (OSError,ValueError,KeyError) as error:
            run.update(state='blocked',error=type(error).__name__,reason='inventory_requires_attention');self.persist(run)
