"""Recover paid proposals into a NEW unaccepted derivative; never spend/reset.

The original response, original request, generation delivery, frozen source,
archived runtime and pre-repair plan are verified before an offline worker is
allowed to revalidate proposals against corrected native extraction. Missing
responses remain explicit gaps, not fabricated provider receipts.
"""
import argparse
import ast
import json
from pathlib import Path
import sqlite3
import shutil
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,canonical,code_hash,digest,object_hash,read_json
from docker_runner import DockerRunner,linux_path
from model_broker import provider_payload
from repair_ops import POLICY,request_body
from review_protocol import checked_file
from stage_handlers import previous_artifact

CODE='''
import sys,json
from pathlib import Path
sys.path.insert(0,'/app/runtime')
import pymupdf as fitz
from core import atomic,digest
from source_text import ordered_text,EXTRACTOR_VERSION
from formula_preservation import preserve
from vector_reflow import reflow
from repair_ops import inventory,sanitize_response,apply_repairs
from container_worker import seed_assets
seed_assets()
old=json.loads(Path('/input/plan.json').read_text());paid=json.loads(Path('/input/responses.json').read_text())
mapping=json.loads(Path('/input/mapping.json').read_text())['sourcePages']
formula=preserve('/input/source.pdf','/input/initial.pdf',mapping,'/output/formulas.pdf')
mapping,relocations=reflow('/output/formulas.pdf',mapping,'/output/baseline.pdf')
with fitz.open('/input/source.pdf') as doc:texts={str(p):ordered_text(doc[p-1]) for p in old['sourcePages']}
plan={**old,'monoSha256':digest('/output/baseline.pdf'),'sourceTexts':texts,'blocks':inventory('/output/baseline.pdf',mapping)}
responses={};returned=set();counts={'proposed':0,'retained':0,'rejected':0}
for item in paid:
 page=item['sourcePage'];returned.add(page);response=sanitize_response(plan,page,item['response']);responses[page]=response
 proposed=sum(len(item['response'][k]) for k in ('updates','supplements'));retained=sum(len(response[k]) for k in ('updates','supplements'))
 counts['proposed']+=proposed;counts['retained']+=retained;counts['rejected']+=proposed-retained
missing=sorted(set(plan['sourcePages'])-returned)
for page in missing:
 responses[page]={'candidateId':plan['candidateId'],'sourcePage':page,'updates':[],'supplements':[],
  'unresolved':['No paid provider response exists for this page. No change applied; complete fresh review and bounded repair remain required.']}
previous=json.loads(Path('/input/supplements.json').read_text())
files=apply_repairs('/output/baseline.pdf',mapping,plan,responses,'/output',previous)
atomic('/output/replay-plan.json',plan)
atomic('/output/replay-result.json',{'files':files,'sourceSha256':digest('/input/source.pdf'),
 'sourcePages':plan['sourcePages'],'repairRoundUsed':old['repairRound'],'returnedPages':sorted(returned),
 'missingProviderPages':missing,'counts':counts,'extractorVersion':EXTRACTOR_VERSION,
 'formulaPreservation':formula,'reflowRegions':len(relocations),'acceptance':'not_granted',
 'modelCalls':0,'fullIndependentReviewRequired':True})
'''


def archived_system(runtime,expected_hash):
    files={p.name:digest(p) for p in sorted(Path(runtime).glob('*.py'))}
    # Match the same manifest algorithm as core.code_hash, without importing or
    # executing the archived application or any paper-provided content.
    actual=object_hash(files)
    if actual!=expected_hash:raise ValueError('Archived producer runtime changed')
    tree=ast.parse((Path(runtime)/'repair_ops.py').read_text(encoding='utf-8'))
    values={}
    for node in tree.body:
        if (isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)
            and node.targets[0].id in ('SYSTEM','POLICY')):
            values[node.targets[0].id]=ast.literal_eval(node.value)
        elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name) and node.target.id=='SYSTEM':
            if not isinstance(node.op,ast.Add):raise ValueError('Unsupported archived system prompt construction')
            values['SYSTEM']+=ast.literal_eval(node.value)
    if values['POLICY']!=POLICY:raise ValueError('Historical repair policy differs')
    return values['SYSTEM']


def verify_calls(store,job,phase,plan,old_system):
    private=store.root/'broker-private';folder=Path(job['candidate']['files']['cn']['path']).parent
    checked_file(job['candidate']['files']['cn'])
    rows=store.db.execute('''SELECT DISTINCT c.body FROM model_calls c JOIN model_deliveries d ON c.id=d.call_id
     WHERE d.job_id=? AND d.stage='repair' AND d.generation=?''',(job['id'],phase['generation'])).fetchall()
    verified={}
    for raw, in rows:
        call=json.loads(raw)
        if call['state']!='completed':continue
        if call['job']!=job['id'] or call['stage']!='repair' or call['policy']!=POLICY:raise ValueError('Wrong repair provider receipt')
        request=read_json(checked_file(call['request'],private));content=request['messages'][1]['content']
        context=json.loads(content[0]['text'] if isinstance(content,list) else content);page=context['sourcePage']
        body=request_body(plan,page,folder);body['messages'][0]['content']=old_system
        # Historical workers supplied the image bytes/names but not a redundant
        # JSON list of their hashes. Match that archived request schema exactly.
        old_code=(Path(store.archived_runtime)/'repair_ops.py').read_text(encoding='utf-8') if hasattr(store,'archived_runtime') else ''
        if "unit['sourceImages']=" not in old_code:
            context_text=json.loads(body['messages'][1]['content'][0]['text']) if isinstance(body['messages'][1]['content'],list) else json.loads(body['messages'][1]['content'])
            context_text.pop('sourceImages',None)
            encoded=json.dumps(context_text,ensure_ascii=False)
            if isinstance(body['messages'][1]['content'],list):body['messages'][1]['content'][0]['text']=encoded
            else:body['messages'][1]['content']=encoded
        if request!=provider_payload(body,POLICY):raise ValueError('Paid response has a different original source/context')
        raw_response=read_json(checked_file(call['response'],private))
        response=json.loads(raw_response['choices'][0]['message']['content'])
        if response.get('candidateId')!=plan['candidateId'] or response.get('sourcePage')!=page:raise ValueError('Paid response identity differs')
        item={'sourcePage':page,'call':call['id'],'request':call['request'],'responseReceipt':call['response'],'response':response}
        if page in verified and canonical(verified[page]['response'])!=canonical(response):raise ValueError('Ambiguous paid responses for one page')
        verified[page]=item
    if not verified:raise ValueError('No paid repair responses to recover')
    return [verified[p] for p in sorted(verified)]


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--job',required=True)
    p.add_argument('--translation-job',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    config=read_json(a.config);data=Path(config['data']);db=sqlite3.connect((data/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    store=SimpleNamespace(root=data,db=db,archived_runtime=config['runtime']);job=read_json_value(db,'jobs',a.job);phase=read_json_value(db,'phase_jobs',a.job,'job_id')
    batch=read_json_value(db,'batches',phase['batch'])
    if batch['state']!='paused' or phase['state']=='running' or phase['stage']!='repair':raise ValueError('Pause and settle the original repair worker first')
    if not 1<=phase['repairRound']<=2:raise ValueError('Original semantic repair count is required')
    source=checked_file({'path':job['source'],'sha256':job['sourceSha256']})
    old_plan_path=data/'jobs'/job['id']/'stages'/f"lit-{job['id']}-repair-{phase['generation']}"/'repair-plan.json'
    plan=read_json(old_plan_path)
    original=read_json(data/'jobs'/job['id']/'repair-inputs'/f"generation-{phase['generation']}.json")
    if {k:v for k,v in plan.items() if k!='blocks'}!=original:raise ValueError('Frozen pre-repair plan changed')
    if plan['sourceSha256']!=job['sourceSha256'] or plan['candidateId']!=job['candidate']['id']:raise ValueError('Original repair source/candidate differs')
    if digest(previous_artifact(store,phase,'mono'))!=plan['monoSha256']:raise ValueError('Original repair mono changed')
    system=archived_system(config['runtime'],job['candidate']['codeHash']);paid=verify_calls(store,job,phase,plan,system)
    initial=read_json_value(db,'jobs',a.translation_job);initial_phase=read_json_value(db,'phase_jobs',a.translation_job,'job_id')
    if initial['sourceSha256']!=job['sourceSha256'] or initial['sourcePages']!=job['sourcePages']:raise ValueError('Initial translation source differs')
    checkpoint=read_json_value(db,'phase_checkpoints',initial_phase['lastCheckpoint'])
    if checkpoint['stage']!='translate' or object_hash(checkpoint)!=initial_phase['lastCheckpoint']:raise ValueError('Complete initial translation required')
    artifacts=checkpoint['artifacts'];mono=checked_file(artifacts['mono']);mapping=checked_file(artifacts['mapping']);checked_file(artifacts['translationCalls'])
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False);inputs=out/'inputs';inputs.mkdir();output=out/'output';output.mkdir()
    atomic(inputs/'plan.json',plan);atomic(inputs/'responses.json',paid)
    try:supplements=read_json(previous_artifact(store,phase,'supplements'))
    except ValueError:supplements=[]
    atomic(inputs/'supplements.json',supplements);(inputs/'probe.py').write_text(CODE,encoding='utf-8')
    frozen_runtime=out/'runtime';shutil.copytree(ROOT/'runtime',frozen_runtime,ignore=shutil.ignore_patterns('__pycache__'))
    runner=DockerRunner(out,frozen_runtime,config['imageLock'],{})
    name='lit-paid-replay-'+str(time.time_ns())
    args=['run','--detach','--pull','never','--name',name,'--network','none','--read-only','--user','10001:10001',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','2g','--cpus','2','--pids-limit','128',
          '--tmpfs','/tmp:rw,noexec,nosuid,size=128m','--env','HOME=/tmp/literature-home']
    for src,dst,ro in [(frozen_runtime,'/app/runtime',True),(inputs,'/input/replay',True),(source,'/input/source.pdf',True),
                       (mono,'/input/initial.pdf',True),(mapping,'/input/mapping.json',True),(output,'/output',False)]:
        args.extend(['--mount',f'type=bind,source={linux_path(src)},target={dst}'+(',readonly' if ro else '')])
    # Inputs are a fixed local bundle; use explicit mounts for worker constants.
    for name_in in ('plan.json','responses.json','supplements.json'):
        args.extend(['--mount',f'type=bind,source={linux_path(inputs/name_in)},target=/input/{name_in},readonly'])
    args.extend([runner.image,'python','/input/replay/probe.py'])
    origin={'sourceJob':job['id'],'sourceSha256':job['sourceSha256'],'sourcePages':job['sourcePages'],
            'originalCandidate':plan['candidateId'],'initialTranslationJob':initial['id'],'translationCheckpoint':initial_phase['lastCheckpoint'],
            'oldRuntimeHash':job['candidate']['codeHash'],'oldRuntimePath':config['runtime'],
            'newRuntimeHash':object_hash({p.name:digest(p) for p in sorted(frozen_runtime.glob('*.py'))}),'repairRoundUsed':phase['repairRound'],
            'providerCalls':[{'call':r['call'],'sourcePage':r['sourcePage'],'request':r['request'],'response':r['responseReceipt']} for r in paid],
            'worker':name,'image':runner.image,'modelCalls':0,'acceptance':'not_granted','createdAt':time.time()}
    origin['inputs']={p.name:digest(p) for p in inputs.iterdir() if p.is_file()}
    atomic(out/'origin.json',origin);db.close()
    result=runner.docker(args,timeout=30)
    if result.returncode:raise RuntimeError('Replay launch acknowledgment missing; inspect the recorded worker before retrying')
    print(json.dumps({'worker':name,'folder':str(out),'paidPages':len(paid),'repairRoundUsed':phase['repairRound'],'modelCalls':0}))


def read_json_value(db,table,identifier,column='id'):
    row=db.execute(f'SELECT body FROM {table} WHERE {column}=?',(identifier,)).fetchone()
    if row is None:raise ValueError('Missing frozen state in '+table)
    return json.loads(row[0])


if __name__=='__main__':main()
