"""Continue only the original remaining list after a bounded failed pilot.

This explicit maintenance route does not grant general production readiness,
raise a budget, accept a PDF, or authorize a new Zotero connection. It uses the
already recorded start instruction and keeps the deferred paper's issue bundle.
Default is read-only audit; --activate installs and starts the exact remaining
batch after all current workers/costs have reached a known stopped state.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,code_hash,digest,object_hash,read_json
from batches import BatchStore
from budget import BudgetLedger
from codex_client import call
from deployment_install import install,start_hidden,verify_installation
from deployment_policy import DeploymentPolicy
from instance_lock import InstanceLock
from model_gate import ModelGate
from service_main import export_adapters,local_call
from user_authorization import bind_existing_authorization,locate_instruction
from zotero_resolver import ZoteroResolver
from activate_verified_batch import verify_scope


def row(db,table,identifier,column='id'):
    result=db.execute(f'SELECT body FROM {table} WHERE {column}=?',(identifier,)).fetchone()
    if not result:raise ValueError('Missing recorded '+table)
    return json.loads(result[0])


def verify_deferral(job,phase,cost):
    if phase['job']!=job['id'] or phase['state']!='blocked':raise ValueError('The same pilot must finish or block before deferral')
    if phase['repairRound']<2 and not cost['paused']:raise ValueError('Use the remaining bounded repair or wait for its budget result')
    if cost['reserved'] or cost['unknown']:raise ValueError('Unresolved paid pilot requests remain')
    if job.get('acceptance') or job.get('publication'):raise ValueError('An accepted pilot requires its delivery route, not a failure deferral')
    package=phase.get('problemPackage')
    if not package or digest(package['path'])!=package['sha256']:raise ValueError('A complete local issue bundle is required')
    return {'job':job['id'],'sourceSha256':job['sourceSha256'],'sourcePages':job['sourcePages'],
            'stage':phase['stage'],'repairRound':phase['repairRound'],'error':phase.get('error'),
            'problemPackage':package,'acceptanceGranted':False,'spentMicros':cost['spent']}


def audit(config,private):
    pointer=read_json(private/'pilot-redeployment-current.json');bulk_pointer=read_json(private/'first-batch-current.json')
    intake=read_json(private.parent/'first-batch-intake-20261110.json')
    db=sqlite3.connect((Path(config['data'])/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        pilot=row(db,'batches',pointer['pilot']['batch']);bulk=row(db,'batches',bulk_pointer['batch'])
        verify_scope(intake,pilot,bulk)
        job=row(db,'jobs',pointer['pilot']['job']);phase=row(db,'phase_jobs',job['id'],'job_id')
        ledger=BudgetLedger.__new__(BudgetLedger);ledger.db=db;cost=ledger.status(job['budgetScope'])
        deferral=verify_deferral(job,phase,cost)
        if any(json.loads(r[0])['state']=='running' for r in db.execute('SELECT body FROM phase_jobs')):
            raise ValueError('An actual worker is still running; observe the same worker')
        if db.execute("SELECT 1 FROM budget_requests WHERE state IN ('reserved','dispatched','unknown') LIMIT 1").fetchone():
            raise ValueError('Unresolved provider cost remains')
        return intake,pilot,bulk,deferral
    finally:db.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--private',required=True)
    p.add_argument('--releases');p.add_argument('--validation-log');p.add_argument('--activate',action='store_true');a=p.parse_args()
    config_path=Path(a.config).resolve(strict=True);private=Path(a.private).resolve(strict=True);config=read_json(config_path)
    installation=verify_installation(config_path)
    host=ModelGate(config['sessionsRoot'],expected_effort=None).check(os.environ['CODEX_THREAD_ID'])
    locate_instruction(config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
    intake,pilot,old_bulk,deferred=audit(config,private)
    if not a.activate:
        print(json.dumps({'readyForScopedContinuation':True,'activated':False,'remaining':len(old_bulk['items']),
                          'deferred':deferred['job'],'productionReady':False},ensure_ascii=False));return
    if not a.releases or not a.validation_log:raise ValueError('An immutable release root and passing regression log are required')
    log=Path(a.validation_log);text=log.read_text(encoding='utf-8').replace('\r\n','\n')
    if '\nOK\n' not in text or 'FAILED (' in text:raise ValueError('Passing full offline regression required')
    local_call(config_path,stop=True);state_path=config_path.parent/'service-state.json'
    for _ in range(200):
        if read_json(state_path).get('state')=='stopped':break
        time.sleep(.1)
    else:raise RuntimeError('Old service stop not confirmed')
    # Recheck under the single writer lock before changing any registered batch.
    with InstanceLock(config['data']):audit(config,private)
    installed=install(ROOT,a.releases,config_path);config=read_json(config_path)
    with InstanceLock(config['data']):
        store=BatchStore(config['data'])
        try:
            current=store.phase(deferred['job'])
            if current['state']!='blocked' or current.get('problemPackage')!=deferred['problemPackage']:raise ValueError('Deferred pilot changed')
            from issue_packages import create_package
            create_package(store,current)
            deferred=verify_deferral(store.get(deferred['job']),store.phase(deferred['job']),store.budgets.status(store.get(deferred['job'])['budgetScope']))
            keys=[r['zotero']['parentItemKey'] for r in old_bulk['items']]
            inputs,omissions=ZoteroResolver(Path(config['data'])/'library-scan')({'kind':'items','keys':keys},'all')
            if omissions or {r['_sourceInfo']['sourceSha256'] for r in inputs}!={r['sourceSha256'] for r in old_bulk['items']}:
                raise ValueError('The original remaining list or its source files changed')
            prepared=store.prepare(inputs,{**config['jobConfig'],'runtimeRevision':code_hash()},pilot=False,
                                   title='原文献清单其余14篇：有限自动翻译与逐篇验收')
            authorization=bind_existing_authorization(store,prepared['batch'],private.parent/'first-batch-intake-20261110.json',
                                                     config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
            bulk=store.batch(prepared['batch']);verify_scope(intake,pilot,bulk)
            if store.batch(old_bulk['id'])['state'] not in ('complete','cancelled'):store.control(old_bulk['id'],'cancel')
            if store.batch(pilot['id'])['state'] not in ('complete','cancelled'):store.control(pilot['id'],'cancel')
            receipt={'mode':'original-list-bounded','runtimeHash':code_hash(),'imageLockSha256':digest(config['imageLock']),
                     'offlinePipelinePassed':True,'codexHostVerified':True,'confirmationVerified':True,'scopeVerified':True,
                     'expiresAt':time.time()+86400,'host':host,'installation':installed['id'],'previousInstallation':installation['id'],
                     'allowedBatch':{'id':bulk['id'],'summaryHash':bulk['summaryHash'],'sourceHashes':sorted(r['sourceSha256'] for r in bulk['items'])},
                     'authorization':authorization,'deferredPilot':deferred,'regressionLogSha256':digest(log),
                     'routingBasis':'Already authorized original list; failed first paper retained separately; no new source or budget authorization.',
                     'realPilotAccepted':False,'productionReady':False,'newZoteroAuthorizationGranted':False}
            receipt_path=config_path.parent/'original-list-readiness.json';atomic(receipt_path,receipt)
            config['scopedBatchReadinessReceipt']=str(receipt_path);config['paidWorkEnabled']=True;atomic(config_path,config)
            pointer={'batch':bulk['id'],'summaryHash':bulk['summaryHash'],'papers':len(bulk['items']),
                     'pages':sum(len(i['pages']) for i in bulk['items']),'authorization':authorization,'pilot':False,
                     'pilotReference':{'batch':pilot['id'],'job':deferred['job'],'sourceSha256':deferred['sourceSha256'],
                                       'pages':len(deferred['sourcePages']),'countsAsDeliveredOnlyAfterAcceptance':True,'deferred':True,
                                       'problemPackage':deferred['problemPackage']},
                     'fullScopePapers':len(intake['ready']),'fullScopePages':sum(i['pages'] for i in intake['ready']),
                     'scopedContinuationReceipt':{'path':str(receipt_path),'sha256':digest(receipt_path)}}
            atomic(private/'first-batch-current.json',pointer)
            if not DeploymentPolicy(config).allows(store,bulk['id']):raise ValueError('Scoped deployment guard refused the exact batch')
        finally:store.db.close()
    adapters=export_adapters(config_path);started=time.time();start_hidden(config_path)
    for _ in range(200):
        state=read_json(state_path)
        if state.get('state')=='running' and state.get('startedAt',0)>=started:break
        time.sleep(.1)
    else:raise RuntimeError('New service startup remains unconfirmed; inspect existing launch')
    status=call(adapters['codexConfig'],'start_batch',{'batch':bulk['id'],'summaryHash':bulk['summaryHash']})['result']
    report={'batch':bulk['id'],'state':status['state'],'papers':len(bulk['items']),'deferred':deferred['job'],
            'installation':installed['id'],'productionReady':False,'scopedBatchReady':True,'budgetsUnchanged':True}
    atomic(private/'original-list-activation.json',report);print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
