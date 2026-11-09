"""Offline original-formula restoration on frozen copies, zero API requests."""
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
from pathlib import Path
import pymupdf as fitz
from core import atomic,read_json
cache=Path.home()/'.cache/babeldoc';cache.mkdir(parents=True,exist_ok=True)
for folder in ('fonts','models','cmap','tiktoken'):(cache/folder).symlink_to(Path('/opt/engine-home/.cache/babeldoc')/folder,target_is_directory=True)
from formula_preservation import preserve,detect
pages=read_json('/input/pages.json')
with fitz.open('/input/mono.pdf') as doc:
 selected=fitz.open()
 for number in pages:selected.insert_pdf(doc,from_page=number-1,to_page=number-1)
 selected.save('/output/selected-mono.pdf');selected.close()
with fitz.open('/input/source.pdf') as doc:detections=detect(doc,set(pages))
results=[]
for index,sp in enumerate(pages):
 with fitz.open('/output/selected-mono.pdf') as doc:
  selected=fitz.open();selected.insert_pdf(doc,from_page=index,to_page=index);selected.save('/output/input-%04d.pdf'%sp);selected.close()
 try:
  result=preserve('/input/source.pdf','/output/input-%04d.pdf'%sp,[sp],'/output/restored-%04d.pdf'%sp,detections)
  result['sourcePage']=sp;results.append(result)
  with fitz.open('/output/restored-%04d.pdf'%sp) as doc:doc[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save('/output/page-%04d.png'%sp)
 except Exception as e:results.append({'sourcePage':sp,'error':type(e).__name__,'message':str(e)})
 atomic('/output/progress.json',{'completedPages':len(results),'totalPages':len(pages),'modelCalls':0})
atomic('/output/preservation.json',{'modelCalls':0,'sourcePages':pages,'pageReports':results,
 'restoredCount':sum(len(r.get('restored',[])) for r in results),
 'uncertainCount':sum(len(r.get('uncertain',[])) for r in results),
 'errorCount':sum('error' in r for r in results),'acceptance':'not_granted'})
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    parser.add_argument('--job',default='paper-106fc334aa9f4487');parser.add_argument('--pages',default='4');args=parser.parse_args()
    config=read_json(args.config);data=Path(config['data']);db=sqlite3.connect((data/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        job=json.loads(db.execute('SELECT body FROM jobs WHERE id=?',(args.job,)).fetchone()[0])
        phase=json.loads(db.execute('SELECT body FROM phase_jobs WHERE job_id=?',(args.job,)).fetchone()[0])
        checkpoint=json.loads(db.execute('SELECT body FROM phase_checkpoints WHERE id=?',(phase['lastCheckpoint'],)).fetchone()[0])
    finally:db.close()
    if checkpoint['stage']!='translate':raise ValueError('A complete original translate checkpoint is required')
    pages=job['sourcePages'] if args.pages=='all' else [int(p) for p in args.pages.split(',')]
    if any(p not in job['sourcePages'] for p in pages):raise ValueError('Page outside original translation')
    case=ROOT/'.test-output'/('formula-preservation-'+time.strftime('%Y%m%d-%H%M%S'));case.mkdir();out=case/'output';out.mkdir()
    (case/'probe.py').write_text(CODE,encoding='utf-8');(case/'pages.json').write_text(json.dumps(pages),encoding='utf-8')
    runner=DockerRunner(case,ROOT/'runtime',config['imageLock'],{})
    command=['run','--rm','--pull','never','--network','none','--read-only','--user','10001:10001',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','2g','--cpus','2',
        '--tmpfs','/tmp:rw,noexec,nosuid,size=128m','--env','HOME=/tmp/home','--env','PYTHONPATH=/app/runtime']
    mounts=[(ROOT/'runtime','/app/runtime',True),(case/'probe.py','/input/probe.py',True),
        (case/'pages.json','/input/pages.json',True),(Path(job['source']),'/input/source.pdf',True),
        (Path(checkpoint['artifacts']['mono']['path']),'/input/mono.pdf',True),(out,'/output',False)]
    for source,target,ro in mounts:command.extend(['--mount',f'type=bind,source={linux_path(source)},target={target}'+(',readonly' if ro else '')])
    command.extend([runner.image,'python','/input/probe.py']);result=runner.docker(command,timeout=180)
    report=read_json(out/'preservation.json');summary={k:v for k,v in report.items() if k!='pageReports'}
    summary['errors']=[r for r in report.get('pageReports',[]) if r.get('error')]
    print(json.dumps({'evidence':str(out/'preservation.json'),'exitCode':result.returncode,'report':summary},ensure_ascii=False))
    if result.returncode:print(result.stderr[-1000:]);raise SystemExit(1)


if __name__=='__main__':main()
