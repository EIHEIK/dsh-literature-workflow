"""Trusted deployment gates; pilot scope never authorizes normal production.

Receipts are issued by installation/acceptance maintenance, not model tools.
Pilot readiness covers the prerequisites for the experiment, not its outcome.
"""
import re
import time

from core import code_hash,digest,read_json,object_hash


class DeploymentPolicy:
    def __init__(self,config):self.config=config

    def receipt(self,path):
        if not self.config.get('paidWorkEnabled'):return None
        try:
            value=read_json(path)
            if value.get('runtimeHash')!=code_hash() or value.get('imageLockSha256')!=digest(self.config['imageLock']):return None
            return value
        except (OSError,ValueError,KeyError,TypeError):return None

    def production_ready(self):
        value=self.receipt(self.config.get('readinessReceipt'))
        supervisor='codexEndToEnd' if self.config.get('supervisionMode','dot')=='codex' else 'dotEndToEnd'
        return bool(value and all(value.get(field) is True for field in (supervisor,'productionBackend','realPilotAccepted')))

    def pilot_receipt(self):
        value=self.receipt(self.config.get('pilotReadinessReceipt'))
        if not value or value.get('mode')!='pilot':return None
        supervisor='codexHostVerified' if self.config.get('supervisionMode','dot')=='codex' else 'dotHostVerified'
        if not all(value.get(field) is True for field in ('offlinePipelinePassed',supervisor,'confirmationVerified')):return None
        expires=value.get('expiresAt');summaries=value.get('allowedSummaryHashes')
        if type(expires) not in (float,int) or not time.time()<expires<=time.time()+7*86400:return None
        if not isinstance(summaries,list) or not 1<=len(summaries)<=10:return None
        if any(not isinstance(s,str) or not re.fullmatch('[a-f0-9]{64}',s) for s in summaries):return None
        return value

    def pilot_ready(self):return self.pilot_receipt() is not None

    def scoped_batch_receipt(self):
        """A maintenance-only exception for an exact, already authorized list.

        It never asserts that the failed pilot passed or opens general
        production. Every paper still goes through the unchanged acceptance.
        """
        value=self.receipt(self.config.get('scopedBatchReadinessReceipt'))
        if not value or value.get('mode')!='original-list-bounded':return None
        supervisor='codexHostVerified' if self.config.get('supervisionMode','dot')=='codex' else 'dotHostVerified'
        if not all(value.get(k) is True for k in ('offlinePipelinePassed',supervisor,'confirmationVerified','scopeVerified')):return None
        expires=value.get('expiresAt');allowed=value.get('allowedBatch',{})
        if not isinstance(allowed,dict):return None
        if type(expires) not in (int,float) or not time.time()<expires<=time.time()+7*86400:return None
        if (not re.fullmatch('batch-[a-f0-9]{16,32}',allowed.get('id','')) or
            not re.fullmatch('[a-f0-9]{64}',allowed.get('summaryHash',''))):return None
        hashes=allowed.get('sourceHashes',[])
        if not isinstance(hashes,list) or not 1<=len(hashes)<=100:return None
        if any(not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h) for h in hashes):return None
        if len(set(hashes))!=len(hashes):return None
        try:
            from pathlib import Path
            deferred=value['deferredPilot'];package=Path(deferred['problemPackage']['path']).resolve(strict=True)
            if not re.fullmatch('paper-[a-f0-9]{16,32}',deferred['job']):return None
            root=Path(self.config['data']).resolve()/ 'jobs'/deferred['job']/'problem-packages'
            if not package.is_relative_to(root) or digest(package)!=deferred['problemPackage']['sha256']:return None
            if deferred['sourceSha256'] in hashes:return None
            auth=value['authorization'];auth_path=Path(auth['receipt']).resolve(strict=True)
            if not auth_path.is_relative_to(Path(self.config['data']).resolve()/'user-authorizations'):return None
            body=read_json(auth_path)
            if (object_hash(body)!=auth['receiptHash'] or body['batch']!=allowed['id'] or
                body['summaryHash']!=allowed['summaryHash'] or body['runtimeHash']!=code_hash() or
                {r['sourceSha256'] for r in body['items']}!=set(hashes)):return None
        except (OSError,ValueError,KeyError,TypeError):return None
        return value

    def scoped_batch_ready(self):return self.scoped_batch_receipt() is not None

    def enabled(self):return self.production_ready() or self.pilot_ready() or self.scoped_batch_ready()

    def allows(self,store,batch_id):
        if self.production_ready():return True
        scoped=self.scoped_batch_receipt()
        if scoped:
            batch=store.batch(batch_id);allowed=scoped['allowedBatch']
            if (batch['id']==allowed['id'] and batch['summaryHash']==allowed['summaryHash'] and
                batch['runtimeHash']==code_hash() and batch.get('pilot') is False and batch.get('pilotCap') is None and
                batch.get('perPaperCap')==10_000_000 and batch.get('perPaperTarget')==5_000_000 and
                batch.get('batchCap') is None and batch.get('confirmation') and
                batch['confirmation']['receipt']=='codex-user-instruction:'+scoped['authorization']['receiptHash'] and
                sorted(r['sourceSha256'] for r in batch['items'])==sorted(allowed['sourceHashes'])):return True
        value=self.pilot_receipt()
        if not value:return False
        batch=store.batch(batch_id)
        return (batch.get('pilot') is True and batch.get('pilotCap')==15_000_000 and
                batch.get('perPaperCap')==10_000_000 and batch.get('runtimeHash')==code_hash() and
                batch.get('summaryHash') in value['allowedSummaryHashes'])

    def guard(self,store):
        for batch_id, in store.db.execute('SELECT id FROM batches').fetchall():
            if store.batch(batch_id)['state']=='running' and not self.allows(store,batch_id):
                store.control(batch_id,'pause')
                with store.transaction():store.event('deployment.paused',batch_id,reason='readiness_or_pilot_scope_invalid')
