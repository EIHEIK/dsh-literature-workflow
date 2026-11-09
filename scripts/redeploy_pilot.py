"""Maintenance-only tested revision deployment, preserving all costs and scope."""
import argparse
import json
import os
import sqlite3
import shutil
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,code_hash,digest,read_json,object_hash
from batches import BatchStore
from codex_client import call
from deployment_install import install,start_hidden
from instance_lock import InstanceLock
from legacy_import import prepare_pilot
from model_gate import ModelGate
from model_broker import PriceBook
from service_main import local_call,export_adapters
from user_authorization import bind_existing_authorization
from zotero_resolver import ZoteroResolver


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--private',required=True)
    p.add_argument('--legacy',required=True);p.add_argument('--releases',required=True);p.add_argument('--validation-log',required=True)
    p.add_argument('--fullpaper-parent');p.add_argument('--reuse-translation-job');p.add_argument('--reuse-repair-package');a=p.parse_args()
    config_path=Path(a.config).resolve();private=Path(a.private).resolve();log=Path(a.validation_log)
    text=log.read_text(encoding='utf-8')
    if '\nOK\n' not in text.replace('\r\n','\n') or 'FAILED (' in text:raise ValueError('A passing regression log is required')
    config=read_json(config_path);state_path=config_path.parent/'service-state.json'
    intake=private.parent/'first-batch-intake-20261110.json'
    repair_package=None
    if a.reuse_repair_package:
        if not a.fullpaper_parent or not a.reuse_translation_job:raise ValueError('Repair reuse requires the original full-paper translation')
        from finalize_repair_replay import finalize
        folder=Path(a.reuse_repair_package).resolve(strict=True).parent
        sealed=finalize(config_path,folder);repair_package=read_json(sealed['package'])
        origin=read_json(folder/'origin.json')
        if origin['initialTranslationJob']!=a.reuse_translation_job:raise ValueError('Repair package is based on a different initial translation')
    # Observe before stopping: maintenance must not interrupt a live batch just
    # to discover afterwards that its worker could not safely be upgraded.
    observer=sqlite3.connect((Path(config['data'])/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        if any(json.loads(r[0])['state']=='running' for r in observer.execute('SELECT body FROM batches')):
            raise ValueError('A batch is running; observe it to terminal/pause before deployment')
    finally:observer.close()
    # Require a live authenticated service, then observe its actual normal stop.
    local_call(config_path,stop=True)
    for _ in range(200):
        if read_json(state_path).get('state')=='stopped':break
        time.sleep(.1)
    else:raise RuntimeError('Service stop remains unconfirmed')
    with InstanceLock(config['data']):
        store=BatchStore(config['data'])
        try:
            phases=[json.loads(r[0]) for r in store.db.execute('SELECT body FROM phase_jobs')]
            if any(r['state']=='running' for r in phases):raise ValueError('A registered worker still needs observation before upgrading')
            prior=store.budgets.status('pilot:first')
            if prior['reserved'] or prior['unknown']:raise ValueError('Unresolved provider cost must be reconciled first')
            # Rebuild only native extraction when its version changes. The old
            # immutable cache/FTS evidence stays; new background retrieval uses
            # only the versioned corrected paragraphs.
            from library_index import PDFScanner,import_attachment
            from zotero import Zotero
            from source_text import EXTRACTOR_VERSION
            scanner=PDFScanner(Path(config['data'])/'library-scan',ROOT/'runtime',config['imageLock'])
            client=Zotero();indexed=[]
            for item in read_json(intake)['ready']:
                result=import_attachment(store,client,scanner,item['zotero_key'],item['attachmentKey'],expected_sha=item['sourceSha256'])
                if result['status']!='indexed':raise ValueError('Original first-batch source has no usable native text')
                indexed.append({'id':item['id'],**result})
                atomic(private/'library-index-current.json',{'extractorVersion':EXTRACTOR_VERSION,'items':indexed,'expected':15,'modelCalls':0,'zoteroWrites':0})
        finally:store.db.close()
    installed=install(ROOT,a.releases,config_path);config=read_json(config_path)
    with InstanceLock(config['data']):
        store=BatchStore(config['data'])
        try:
            if a.fullpaper_parent:
                selected,missing=ZoteroResolver(Path(config['data'])/'library-scan')({'kind':'items','keys':[a.fullpaper_parent]},'all')
                if missing or len(selected)!=1:raise ValueError('Full-paper pilot source unavailable')
                pilot_config={**config['jobConfig'],'runtimeRevision':code_hash()}
                if a.reuse_translation_job:
                    old=store.get(a.reuse_translation_job);phase=store.phase(old['id'])
                    row=store.db.execute('SELECT body FROM phase_checkpoints WHERE id=?',(phase.get('lastCheckpoint'),)).fetchone()
                    if row is None:raise ValueError('A completed translation checkpoint is required')
                    checkpoint=json.loads(row[0])
                    if object_hash(checkpoint)!=phase['lastCheckpoint'] or checkpoint['stage']!='translate' or checkpoint['sourceSha256']!=selected[0]['_sourceInfo']['sourceSha256']:
                        raise ValueError('Translation checkpoint identity changed')
                    artifacts=checkpoint['artifacts']
                    for label in ('mono','mapping','translationCalls'):
                        if digest(artifacts[label]['path'])!=artifacts[label]['sha256']:raise ValueError('Original translation evidence changed')
                    mapping=read_json(artifacts['mapping']['path'])['sourcePages']
                    if mapping!=old['sourcePages'] or old['zotero']['parentItemKey']!=a.fullpaper_parent:
                        raise ValueError('Translation source scope changed')
                    origin={'sourceJob':old['id'],'checkpoint':phase['lastCheckpoint'],'translationCallReceipt':artifacts['translationCalls']['sha256']}
                    folder=Path(config['data'])/'imports'/object_hash(origin);folder.mkdir(parents=True,exist_ok=True)
                    target=folder/'translated-mono.pdf'
                    if not target.exists():shutil.copyfile(artifacts['mono']['path'],target)
                    if digest(target)!=artifacts['mono']['sha256']:raise ValueError('Frozen translation copy changed')
                    atomic(folder/'origin.json',origin)
                    pilot_config['importTranslation']={'path':str(target),'sha256':digest(target),'mapping':mapping,'originHash':object_hash(origin)}
                if repair_package:
                    from repaired_import import validate_import,EXTRAS
                    receipt=Path(a.reuse_repair_package).resolve();output=receipt.parent/'output'
                    old_repair=store.get(repair_package['sourceJob']);old_phase=store.phase(old_repair['id'])
                    if (old_repair['sourceSha256']!=selected[0]['_sourceInfo']['sourceSha256'] or
                        old_repair['zotero']['parentItemKey']!=a.fullpaper_parent or old_phase['repairRound']!=repair_package['repairRoundUsed']):
                        raise ValueError('Original repair identity or round changed')
                    mapping=read_json(output/'mapping.json')['sourcePages']
                    imported={'path':str(output/'mono.pdf'),'sha256':repair_package['files']['mono.pdf'],
                              'mapping':mapping,'originHash':object_hash(repair_package),'repairRoundUsed':repair_package['repairRoundUsed'],
                              'repairPackage':{'path':str(receipt),'sha256':digest(receipt)}}
                    for label,name in EXTRAS.items():imported[label]={'path':str(output/name),'sha256':repair_package['files'][name]}
                    validate_import(imported,old_repair['sourceSha256'],old_repair['sourcePages'])
                    pilot_config['importTranslation']=imported
                    old_batch=store.batch(old_phase['batch'])
                    if old_batch['state'] not in ('cancelled','complete'):store.control(old_batch['id'],'cancel')
                prepared=store.prepare(selected,pilot_config,pilot=True,title='原清单首篇全文端到端试点')
                authorization=bind_existing_authorization(store,prepared['batch'],intake,config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
                pilot={'batch':prepared['batch'],'summaryHash':prepared['summaryHash'],'job':prepared['items'][0]['job'],'authorization':authorization,'fullPaper':True}
                atomic(Path(config['data'])/'pilot-preparation.json',pilot)
            else:pilot=prepare_pilot(store,a.legacy,intake,config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
            inventory=read_json(intake);scope={'kind':'items','keys':[r['zotero_key'] for r in inventory['ready']]}
            inputs,omissions=ZoteroResolver(Path(config['data'])/'library-scan')(scope,'all')
            if omissions or {r['_sourceInfo']['sourceSha256'] for r in inputs}!={r['sourceSha256'] for r in inventory['ready']}:raise ValueError('Original first-batch sources changed')
            pilot_reference=None
            if a.reuse_translation_job:
                source_sha=selected[0]['_sourceInfo']['sourceSha256']
                inputs=[item for item in inputs if item['_sourceInfo']['sourceSha256']!=source_sha]
                pilot_reference={'batch':pilot['batch'],'job':pilot['job'],'sourceSha256':source_sha,'pages':len(set(mapping)),'countsAsDeliveredOnlyAfterAcceptance':True}
            bulk=store.prepare(inputs,{**config['jobConfig'],'runtimeRevision':code_hash()},pilot=False,title='首批15篇381页全文自动翻译')
            auth=bind_existing_authorization(store,bulk['batch'],intake,config['sessionsRoot'],os.environ['CODEX_THREAD_ID'])
            previous=read_json(private/'first-batch-current.json')
            if previous['batch']!=bulk['batch']:
                state=store.batch(previous['batch'])['state']
                if state not in ('cancelled','complete'):store.control(previous['batch'],'cancel')
            current=store.status(pilot['batch'])
            if current['pilotBudget']['spent']!=prior['spent']:
                raise ValueError('Historical pilot costs were not preserved')
            atomic(private/'first-batch-current.json',{'batch':bulk['batch'],'summaryHash':bulk['summaryHash'],
                'papers':len(bulk['items']),'pages':sum(len(i['pages']) for i in bulk['items']),'authorization':auth,'pilot':False,
                'pilotReference':pilot_reference,'fullScopePapers':len(inventory['ready']),'fullScopePages':sum(item['pages'] for item in inventory['ready'])})
        finally:store.db.close()
    PriceBook(read_json(config['priceBook'])).check_current()
    host=ModelGate(config['sessionsRoot'],expected_effort=None).check(os.environ['CODEX_THREAD_ID'])
    atomic(config['pilotReadinessReceipt'],{'mode':'pilot','runtimeHash':code_hash(),'imageLockSha256':digest(config['imageLock']),
        'offlinePipelinePassed':True,'codexHostVerified':True,'confirmationVerified':True,'allowedSummaryHashes':[pilot['summaryHash']],
        'expiresAt':time.time()+86400,'host':host,'previousSpentMicros':prior['spent'],'regressionLogSha256':digest(log)})
    config['paidWorkEnabled']=True;atomic(config_path,config);adapters=export_adapters(config_path)
    started=time.time();launcher=start_hidden(config_path)
    for _ in range(200):
        state=read_json(state_path)
        if state.get('state')=='running' and state.get('startedAt',0)>=started:break
        time.sleep(.1)
    else:raise RuntimeError('New launcher remains unconfirmed; inspect it before retrying')
    result=call(adapters['codexConfig'],'start_batch',{'batch':pilot['batch'],'summaryHash':pilot['summaryHash']})
    report={'installation':installed['id'],'release':installed['release'],'pilot':pilot,'bulkBatch':bulk['batch'],
            'priorCostPreserved':prior['spent'],'started':result['result']['state']=='running'}
    atomic(private/'pilot-redeployment-current.json',report)
    print(json.dumps({k:report[k] for k in ('installation','release','bulkBatch','priorCostPreserved','started')},ensure_ascii=False))


if __name__=='__main__':main()
