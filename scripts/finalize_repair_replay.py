"""Seal a completed isolated replay for explicit import, without acceptance."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,digest,object_hash,read_json
from docker_runner import DockerRunner,linux_path
from replay_paid_repairs import archived_system,verify_calls,read_json_value


def finalize(config_path,folder):
    config=read_json(config_path);folder=Path(folder).resolve(strict=True);origin=read_json(folder/'origin.json')
    runtime=folder/'runtime'
    if object_hash({p.name:digest(p) for p in sorted(runtime.glob('*.py'))})!=origin['newRuntimeHash']:
        raise ValueError('Replay runtime changed')
    if {p.name:digest(p) for p in (folder/'inputs').iterdir() if p.is_file()}!=origin['inputs']:
        raise ValueError('Replay inputs changed')
    runner=DockerRunner(folder,runtime,config['imageLock'],{});r=runner.docker(['inspect',origin['worker']])
    if r.returncode:raise ValueError('Replay worker identity unavailable')
    state=json.loads(r.stdout)[0];host=state['HostConfig']
    if (state['State']['Running'] or state['State']['ExitCode']!=0 or state['Image']!=origin['image'] or
        state['Config']['User']!='10001:10001' or host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or
        host.get('Privileged') or host.get('CapAdd') or 'ALL' not in (host.get('CapDrop') or []) or
        not any(x.startswith('no-new-privileges') for x in host.get('SecurityOpt',[]))):raise ValueError('Replay completion or isolation is invalid')
    data=Path(config['data']);db=sqlite3.connect((data/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        store=SimpleNamespace(root=data,db=db,archived_runtime=origin['oldRuntimePath']);job=read_json_value(db,'jobs',origin['sourceJob'])
        phase=read_json_value(db,'phase_jobs',origin['sourceJob'],'job_id')
        if phase['state']=='running' or phase['repairRound']!=origin['repairRoundUsed']:raise ValueError('Original repair changed')
        plan=read_json(folder/'inputs/plan.json')
        generations={read_json_value(db,'model_calls',r['call'])['generation'] for r in origin['providerCalls']}
        if len(generations)!=1 or phase['generation']<next(iter(generations)) or job['candidate']['id']!=origin['originalCandidate']:
            raise ValueError('Original provider generation or candidate changed')
        paid=verify_calls(store,job,{**phase,'generation':next(iter(generations))},plan,archived_system(origin['oldRuntimePath'],origin['oldRuntimeHash']))
        if paid!=read_json(folder/'inputs/responses.json'):raise ValueError('Paid provider proof changed')
        expected_mounts={
            '/app/runtime':(linux_path(runtime),False),'/input/replay':(linux_path(folder/'inputs'),False),
            '/input/source.pdf':(linux_path(job['source']),False),'/output':(linux_path(folder/'output'),True)}
        checkpoint=read_json_value(db,'phase_checkpoints',origin['translationCheckpoint'])
        for label,destination in [('mono','/input/initial.pdf'),('mapping','/input/mapping.json')]:
            entry=checkpoint['artifacts'][label]
            if digest(entry['path'])!=entry['sha256']:raise ValueError('Initial translation changed')
            expected_mounts[destination]=(linux_path(entry['path']),False)
        for name in ('plan.json','responses.json','supplements.json'):
            expected_mounts['/input/'+name]=(linux_path(folder/'inputs'/name),False)
        actual={m['Destination']:(m['Source'],m['RW']) for m in state['Mounts'] if m['Type']=='bind'}
        if actual!=expected_mounts or any(m['Type']!='bind' and not(m['Type']=='tmpfs' and m['Destination']=='/tmp') for m in state['Mounts']):
            raise ValueError('Replay mounts changed')
    finally:db.close()
    output=folder/'output';result=read_json(output/'replay-result.json')
    expected={'mono.pdf','mapping.json','supplements.json','repair-applied.json','source-quotes.json'}
    if (set(result['files'])!=expected or result['sourceSha256']!=origin['sourceSha256'] or result['sourcePages']!=origin['sourcePages'] or
        result['repairRoundUsed']!=origin['repairRoundUsed'] or result['acceptance']!='not_granted' or
        result['modelCalls']!=0 or result['fullIndependentReviewRequired'] is not True):raise ValueError('Replay result identity changed')
    for name,sha in result['files'].items():
        if digest(output/name)!=sha:raise ValueError('Replay artifact changed')
    returned=sorted(r['sourcePage'] for r in paid)
    if result['returnedPages']!=returned or result['missingProviderPages']!=sorted(set(origin['sourcePages'])-set(returned)):
        raise ValueError('Replay provider coverage changed')
    mapping=read_json(output/'mapping.json')['sourcePages'];groups=[]
    for page in mapping:
        if not groups or groups[-1]!=page:groups.append(page)
    if groups!=origin['sourcePages']:raise ValueError('Replay source page groups changed')
    package={k:result[k] for k in ('files','sourceSha256','sourcePages','repairRoundUsed','returnedPages','missingProviderPages','counts',
                                 'acceptance','fullIndependentReviewRequired','modelCalls')}
    package.update(originSha256=digest(folder/'origin.json'),resultSha256=digest(output/'replay-result.json'),
                   sourceJob=origin['sourceJob'],producerRuntimeHash=origin['newRuntimeHash'])
    atomic(folder/'package.json',package);return {'package':str(folder/'package.json'),'sha256':digest(folder/'package.json'),**result['counts']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--folder',required=True);a=p.parse_args()
    print(json.dumps(finalize(a.config,a.folder),ensure_ascii=False))
