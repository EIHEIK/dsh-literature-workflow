"""Validate the real legacy translation in a network-none worker, no job dispatch."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,digest,read_json
from docker_runner import DockerRunner,linux_path

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=read_json(args.config);data=Path(config['data']);prep=read_json(data/'pilot-preparation.json')
    import sqlite3
    db=sqlite3.connect((data/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    job=json.loads(db.execute('SELECT body FROM jobs WHERE id=?',(prep['job'],)).fetchone()[0]);db.close()
    imported=job['config']['importTranslation'];folder=ROOT/'.test-output'/('legacy-import-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir()
    ipc=folder/'ipc';ipc.mkdir();work=folder/'work';work.mkdir()
    stage={'schema':1,'job':job['id'],'generation':1,'stage':'translate','sourceSha256':job['sourceSha256'],
           'sourcePages':job['sourcePages'],'credential':'offline-import-fixture-not-a-provider-key',
           'settings':{'mode':'import_translation','sha256':imported['sha256'],'mapping':imported['mapping']}}
    atomic(folder/'stage.json',stage)
    runner=DockerRunner(folder,config['runtime'],config['imageLock'],{});name='lit-import-check-'+str(time.time_ns())
    command=['run','--pull','never','--name',name,'--network','none','--read-only','--user','10001:10001',
             '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','1g','--cpus','1','--pids-limit','128',
             '--tmpfs','/tmp:rw,noexec,nosuid,size=64m','-e','HOME=/tmp/literature-home']
    for source,target,readonly in [(config['runtime'],'/app/runtime',True),(folder/'stage.json','/input/stage.json',True),
        (job['source'],'/input/source.pdf',True),(imported['path'],'/input/legacy.pdf',True),(ipc,'/ipc',False),(work,'/work',False)]:
        command.extend(['--mount',f'type=bind,source={linux_path(source)},target={target}'+(',readonly' if readonly else '')])
    command.extend([runner.image,'python','/app/runtime/container_worker.py'])
    process=runner.docker(command,timeout=90)
    if process.returncode:raise RuntimeError('Import container failed; inspect retained container '+name)
    if digest(work/'mono.pdf')!=imported['sha256'] or read_json(work/'mapping.json')['sourcePages']!=imported['mapping']:
        raise ValueError('Real imported translation changed')
    requests=list(ipc.rglob('request.json'))
    if requests:raise ValueError('Import unexpectedly attempted model requests')
    report={'passed':True,'job':job['id'],'container':name,'image':runner.image,'sourcePages':job['sourcePages'],
            'translatedPages':len(imported['mapping']),'translationBytesPreserved':True,'modelCalls':0,'zoteroWrites':0,
            'acceptanceInherited':False,'boundary':'Actual isolated legacy PDF import only, no translation quality claim'}
    atomic(folder/'validation.json',report);print(json.dumps({'evidence':str(folder/'validation.json'),**report},ensure_ascii=False))
