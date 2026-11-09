"""Trusted terminal operations; no model-selected paths or privileged commands.

Docker parses and renders. The host copies already accepted bytes and imports
only into the frozen personal-library parent. Intent/result files make retries
reconcile existing work rather than create duplicate attachments.
"""
from pathlib import Path
import re
import time
import urllib.error

from core import atomic,digest,object_hash,read_json
from zotero import Zotero,archive,key

STAGES=('accept','publish','archive')


class DeliveryRunner:
    def __init__(self,worker_runner,*,zotero_factory=Zotero):
        self.workers=worker_runner;self.zotero_factory=zotero_factory;self.store=None

    def bind(self,store):
        self.store=store;self.workers.bind(store)
        self.control=store.root/'delivery-control';self.control.mkdir(exist_ok=True)

    def storage_ready(self):return self.workers.storage_ready()
    def mailbox_scopes(self,store):return self.workers.mailbox_scopes(store)

    def identity(self,phase):
        if phase['stage'] not in STAGES:return self.workers.identity(phase)
        if not re.fullmatch('paper-[a-f0-9]{16}',phase['job']) or type(phase['generation']) is not int:raise ValueError('Invalid delivery identity')
        return f"host-{phase['job']}-{phase['stage']}-{phase['generation']}"

    def intent_path(self,phase):
        name=phase.get('worker') or self.identity(phase)
        if not re.fullmatch(r'host-paper-[a-f0-9]{16}-(accept|publish|archive)-\d+',name):raise ValueError('Invalid delivery worker')
        return self.control/(name+'.json')

    def start(self,phase,job):
        if phase['stage'] not in STAGES:return self.workers.start(phase,job)
        candidate=self.store.verify(job)
        path=self.intent_path(phase)
        intent={'worker':self.identity(phase),'job':job['id'],'stage':phase['stage'],'generation':phase['generation'],
                'candidate':candidate['id'],'sourceSha256':job['sourceSha256'],'configHash':object_hash(job['config'])}
        if path.exists():
            if read_json(path)!=intent:raise ValueError('Delivery intent changed')
        else:atomic(path,intent)

    def validate_parent(self,job,client):
        z=job.get('zotero') or {}
        if z.get('libraryType','user')!='user':raise ValueError('Only frozen personal-library parents are allowed')
        parent=key(z.get('parentItemKey'));attachment=key(z.get('sourceAttachmentKey'))
        p=client.get('/users/0/items/'+parent);p=p.get('data',p)
        a=client.get('/users/0/items/'+attachment);a=a.get('data',a)
        if p.get('itemType') in (None,'attachment','note') or p.get('deleted') or a.get('parentItem')!=parent or a.get('itemType')!='attachment' or a.get('deleted'):
            raise ValueError('Frozen Zotero parent or source attachment changed')
        if digest(client.file(attachment))!=job['sourceSha256']:raise ValueError('Current Zotero source differs from frozen paper')

    def inspect(self,phase):
        if phase['stage'] not in STAGES:return self.workers.inspect(phase)
        observed={'worker':phase.get('worker') or self.identity(phase),'checkedAt':time.time()}
        path=self.intent_path(phase)
        if not path.exists():return {**observed,'state':'missing'}
        try:
            intent=read_json(path);job=self.store.get(phase['job']);candidate=self.store.verify(job)
            if (intent['job']!=job['id'] or intent['stage']!=phase['stage'] or intent['generation']!=phase['generation']
                or intent['candidate']!=candidate['id'] or intent['sourceSha256']!=job['sourceSha256']
                or intent['configHash']!=object_hash(job['config'])):raise ValueError('Stale delivery intent')
            if phase['stage']=='accept':
                from corpus import CorpusIndex
                corpus=CorpusIndex(self.store.db)
                with self.store.transaction():
                    job=self.store.accept(job['id'])
                    review=read_json(job['acceptance']['reviewFile']['path']).get('corpusReview',{})
                    ids=review.get('verifiedTermIds',[])+review.get('verifiedMemoryIds',[])
                    if ids:corpus.promote(self.store,job['id'],ids)
            elif phase['stage']=='publish':
                # The strict worker already rendered/checked these exact hashes.
                # The controller must not parse untrusted PDFs again.
                if read_json(candidate['files']['qa']['path']).get('profile')!='strict-generated-cn-v1':
                    raise ValueError('Isolated strict PDF QA required for background publishing')
                with self.store.transaction():job=self.store.publish(job['id'],verify_pdf=False)
            else:
                client=self.zotero_factory();self.validate_parent(job,client)
                with self.store.transaction():job=archive(self.store,job['id'],{},client=client)
            result_path=self.store.folder(job)/'delivery'/f"{phase['stage']}-{phase['generation']}.json"
            result={'job':job['id'],'candidate':candidate['id'],'stage':phase['stage'],
                    'acceptance':job['acceptance'],'publication':job['publication'],'archive':job['archive']}
            if result_path.exists() and read_json(result_path)!=result:raise ValueError('Delivery replay differs')
            atomic(result_path,result)
            return {**observed,'state':'completed','artifacts':{'delivery':{'path':str(result_path),'sha256':digest(result_path)}}}
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            # No background host process is left alive after this synchronous
            # operation. A bounded retry can reconcile the durable import intent.
            return {**observed,'state':'terminal','exitCode':1}
        except (ValueError,OSError,KeyError,TypeError):
            return {**observed,'state':'failed','kind':'delivery','message':'Delivery dependency or Zotero reconciliation failed; inspect the local problem package'}

    def cancel(self,phase):
        if phase['stage'] not in STAGES:return self.workers.cancel(phase)
        # Host operations execute on the controller thread, so this method runs
        # only between them, when no import/copy is concurrently in progress.
        return {'state':'terminal','worker':phase.get('worker') or self.identity(phase),'checkedAt':time.time(),'exitCode':0}
