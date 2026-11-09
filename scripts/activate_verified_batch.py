"""Trusted one-shot promotion of the accepted pilot to its existing bulk queue.

This is maintenance code, not an agent tool. It cannot grant acceptance, change
the paper list, or raise budgets. Without --activate it only audits readiness.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def verify_scope(intake, pilot, bulk):
    def identity(row):
        return (row['sourceSha256'], row['zotero']['parentItemKey'], tuple(row['pages']))
    expected={(row['sourceSha256'],row['zotero_key'],tuple(range(1,row['pages']+1))) for row in intake['ready']}
    p=[identity(row) for row in pilot['items']];b=[identity(row) for row in bulk['items']]
    if len(expected)!=len(intake['ready']) or len(p)!=1 or len(set(b))!=len(b):
        raise ValueError('Ambiguous pilot or original list')
    if set(p)&set(b) or set(p+b)!=expected:
        raise ValueError('Pilot and remaining batch must cover the original list exactly once')
    if pilot['pilot'] is not True or bulk['pilot'] is not False:
        raise ValueError('Pilot/production scope changed')
    for batch in (pilot,bulk):
        if batch['perPaperCap']!=10_000_000 or batch['perPaperTarget']!=5_000_000:
            raise ValueError('Per-paper budgets changed')
    if pilot['pilotCap']!=15_000_000 or bulk.get('batchCap') is not None:
        raise ValueError('Batch budgets changed')


def verify_archive(job, client, digest):
    if set(job.get('archive',{}))!={'cn','bi'} or set(job.get('publication',{}).get('outputs',{}))!={'cn','bi'}:
        raise ValueError('Both accepted PDFs must be published and archived')
    parent=job['zotero']['parentItemKey'];children={r['key']:r.get('data',r) for r in client.children(parent)}
    for mode,artifact in job['publication']['outputs'].items():
        receipt=job['archive'][mode]
        if (receipt.get('state')!='verified' or receipt.get('parentItemKey')!=parent or
            receipt.get('publication')!=job['publication']['id'] or receipt.get('sha256')!=artifact['sha256']):
            raise ValueError('Archive receipt does not match the publication')
        data=children.get(receipt['attachmentKey'],{})
        if (data.get('parentItem')!=parent or data.get('itemType')!='attachment' or
            data.get('deleted') or data.get('title')!=receipt['title']):
            raise ValueError('Current Zotero attachment changed')
        if digest(artifact['path'])!=artifact['sha256'] or digest(client.file(receipt['attachmentKey']))!=artifact['sha256']:
            raise ValueError('Published or archived PDF bytes changed')


def verify_terminal(phase, job):
    # The terminal checkpoint retains stage='archive'; there is no 'done' stage.
    if phase['stage']!='archive' or phase['state']!='done' or job.get('state')!='archived':
        raise ValueError('Real pilot delivery did not finish')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    parser.add_argument('--private',required=True);parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    config_path=Path(args.config).resolve(strict=True);config=json.loads(config_path.read_text(encoding='utf-8'))
    # Always audit the installed version which actually produced this candidate.
    sys.path.insert(0,config['runtime'])
    from core import Store,atomic,code_hash,digest,object_hash,read_json
    from codex_client import call
    from deployment_install import verify_installation
    from delivery_runner import DeliveryRunner
    from deployment_policy import DeploymentPolicy
    from model_gate import ModelGate
    from user_authorization import locate_instruction
    from zotero import Zotero
    private=Path(args.private).resolve(strict=True);pilot_pointer=read_json(private/'pilot-redeployment-current.json')
    bulk_pointer=read_json(private/'first-batch-current.json');intake=read_json(private.parent/'first-batch-intake-20261110.json')
    installation=verify_installation(config_path)
    if config.get('supervisionMode')!='codex' or not config.get('paidWorkEnabled'):
        raise ValueError('The verified Codex/API deployment must already be enabled')
    host=ModelGate(config['sessionsRoot'],expected_effort=None).check(os.environ['CODEX_THREAD_ID'])
    instruction=locate_instruction(config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
    adapter=config_path.parent/'codex-adapter.json'
    observed=call(adapter,'get_status',{'batch':pilot_pointer['pilot']['batch']})['result']
    if observed['state']!='complete':raise ValueError('Real pilot is not complete; observe the same worker without restarting')
    store=Store.__new__(Store);store.root=Path(config['data'])
    store.db=sqlite3.connect((store.root/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        def batch(bid):
            row=store.db.execute('SELECT body FROM batches WHERE id=?',(bid,)).fetchone()
            if row is None:raise ValueError('Registered batch not found')
            return json.loads(row[0])
        pilot=batch(pilot_pointer['pilot']['batch']);bulk=batch(bulk_pointer['batch']);verify_scope(intake,pilot,bulk)
        if pilot['runtimeHash']!=code_hash() or bulk['runtimeHash']!=code_hash():raise ValueError('Runtime changed')
        if bulk['summaryHash']!=bulk_pointer['summaryHash'] or not bulk['confirmation']:
            raise ValueError('Original remaining batch is not confirmed')
        auth=bulk_pointer['authorization'];receipt=read_json(auth['receipt'])
        if (object_hash(receipt)!=auth['receiptHash'] or receipt['batch']!=bulk['id'] or
            receipt['summaryHash']!=bulk['summaryHash'] or receipt['instruction']!=instruction or
            receipt['runtimeHash']!=code_hash()):raise ValueError('Original user authorization changed')
        phase=json.loads(store.db.execute('SELECT body FROM phase_jobs WHERE job_id=?',(pilot_pointer['pilot']['job'],)).fetchone()[0])
        job=store.get(pilot_pointer['pilot']['job'])
        verify_terminal(phase,job)
        if not job.get('acceptance') or store.acceptance(job)!=job['acceptance']:
            raise ValueError('Current candidate does not have a matching full acceptance')
        if job['sourcePages']!=list(range(1,job['sourceInfo']['pages']+1)):
            raise ValueError('A partial pilot cannot establish full-paper delivery')
        if observed['pilotBudget']['reserved'] or observed['pilotBudget']['unknown']:
            raise ValueError('Unresolved pilot costs remain')
        client=Zotero();DeliveryRunner.validate_parent(None,job,client);verify_archive(job,client,digest)
        readiness={'runtimeHash':code_hash(),'imageLockSha256':digest(config['imageLock']),
                   'codexEndToEnd':True,'productionBackend':True,'realPilotAccepted':True,
                   'verifiedAt':time.time(),'installation':installation['id'],
                   'host':host,'pilotBatch':pilot['id'],'pilotJob':job['id'],'candidate':job['candidate']['id'],
                   'acceptance':job['acceptance'],'publication':job['publication']['id'],'archive':job['archive'],
                   'remainingBatch':bulk['id'],'remainingSummaryHash':bulk['summaryHash'],
                   'scopeCount':len(intake['ready']),'scopePages':sum(r['pages'] for r in intake['ready'])}
    finally:store.db.close()
    if args.activate:
        if not DeploymentPolicy(config).pilot_ready() and not DeploymentPolicy(config).production_ready():
            raise ValueError('The current deployment must retain its verified readiness receipt')
        atomic(config['readinessReceipt'],readiness)
        result=call(adapter,'start_batch',{'batch':bulk['id'],'summaryHash':bulk['summaryHash']})['result']
        atomic(private/'production-activation.json',{'readinessSha256':digest(config['readinessReceipt']),
               'batch':bulk['id'],'summaryHash':bulk['summaryHash'],'state':result['state'],'at':time.time()})
        print(json.dumps({'ready':True,'started':result['state']=='running','batch':bulk['id'],'papers':len(bulk['items'])}))
    else:print(json.dumps({'ready':True,'activated':False,'remainingPapers':len(bulk['items'])}))


if __name__=='__main__':main()
