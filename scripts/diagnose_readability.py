"""Offline diagnosis of deterministic reflow on a private copy; no job changes."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import read_json
from docker_runner import DockerRunner,linux_path

CODE='''
import sys,json,collections
from pathlib import Path
sys.path.insert(0,'/app/runtime')
from core import atomic,digest,read_json
from repair_ops import inventory,apply_repairs
from vector_reflow import reflow
from formula_preservation import preserve
from pdf_ops import compose
from layout_checks import check_composition
from container_worker import seed_assets
seed_assets()
mapping=read_json('/input/mapping.json')['sourcePages'];pages=sorted(set(mapping))
blocks=inventory('/input/mono.pdf',mapping)
plan={'candidateId':'offline-diagnostic','monoSha256':digest('/input/mono.pdf'),'sourcePages':pages,
      'repairRound':1,'blocks':blocks,'sourceTexts':{str(p):'' for p in pages}}
responses={p:{'candidateId':plan['candidateId'],'sourcePage':p,'updates':[],'supplements':[],'unresolved':[]} for p in pages}
formula_report=preserve('/input/source.pdf','/input/mono.pdf',mapping,'/output/original-formulas.pdf')
atomic('/output/formula-preservation.json',formula_report)
new_mapping,relocations=reflow('/output/original-formulas.pdf',mapping,'/output/vector.pdf')
atomic('/output/relocations.json',relocations)
out=Path('/output/composed');out.mkdir()
layout=compose('/input/source.pdf','/output/vector.pdf',new_mapping,[],out)
qa=check_composition(out,layout,pages)
atomic('/output/diagnosis.json',{'modelCalls':0,'vectorRelocations':len(relocations),'pagesAfter':layout['cnPages'],
 'sourceFormulaRegions':len(formula_report['restored']),'uncertainFormulaRegions':len(formula_report['uncertain']),
 'failures':dict(collections.Counter(f['kind'] for f in qa['hardFailures'])),
 'remainingFontSizes':dict(collections.Counter(str(f.get('size')) for f in qa['hardFailures'] if f['kind']=='generated_chinese_font')),
 'changes':0,
 'boundary':'Deterministic reflow only; not semantic repair or acceptance'})
'''

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();config=read_json(a.config)
    root=Path(config['data']);prep=read_json(root/'pilot-preparation.json')
    db=sqlite3.connect((root/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    job=json.loads(db.execute('SELECT body FROM jobs WHERE id=?',(prep['job'],)).fetchone()[0]);db.close()
    folder=ROOT/'.test-output'/('readability-diagnostic-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir()
    (folder/'probe.py').write_text(CODE,encoding='utf-8');out=folder/'output';out.mkdir()
    (folder/'mapping.json').write_text(json.dumps({'sourcePages':job['config']['importTranslation']['mapping']}),encoding='utf-8')
    runner=DockerRunner(folder,ROOT/'runtime',config['imageLock'],{})
    command=['run','--pull','never','--name','lit-readability-'+str(time.time_ns()),'--network','none','--read-only',
       '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','2g','--cpus','1',
       '--pids-limit','128','--tmpfs','/tmp:rw,noexec,nosuid,size=128m','--env','HOME=/tmp/literature-home']
    for source,target,ro in [(ROOT/'runtime','/app/runtime',True),(folder/'probe.py','/input/probe.py',True),
        (folder/'mapping.json','/input/mapping.json',True),(job['source'],'/input/source.pdf',True),
        (job['config']['importTranslation']['path'],'/input/mono.pdf',True),(out,'/output',False)]:
        command.extend(['--mount',f'type=bind,source={linux_path(source)},target={target}'+(',readonly' if ro else '')])
    command.extend([runner.image,'python','/input/probe.py']);result=runner.docker(command,timeout=120)
    if result.returncode:raise RuntimeError('Offline readability diagnostic failed: '+result.stderr[-1200:])
    print(json.dumps({'evidence':str(out/'diagnosis.json'),**read_json(out/'diagnosis.json')},ensure_ascii=False))
