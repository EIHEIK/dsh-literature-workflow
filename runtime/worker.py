"""Detached worker: restart-safe lease, isolated attempts, bounded API calls."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from core import Store,pdf_info,atomic
from proxy import Meter

REQUIRED=['--openai','--openai-base-url','--openai-api-key','--openai-model','--output','--lang-in','--lang-out','--pages','--translate-table-text']

def probe(engine):
    exe=Path(engine)
    if not exe.is_absolute() or not exe.is_file(): raise ValueError('Configure an absolute pdf2zh_next executable path')
    result=subprocess.run([str(exe),'--help'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    if result.returncode: raise ValueError('pdf2zh_next --help failed')
    missing=[x for x in REQUIRED if x not in result.stdout]
    if missing: raise ValueError('Engine lacks required flags: '+', '.join(missing))
    version=subprocess.run([str(exe),'--version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    if version.returncode: raise ValueError('Engine version probe failed')
    return {'version':(version.stdout or version.stderr).strip()[:300],'help':result.stdout}

def stop_process(child):
    if child.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,timeout=20,creationflags=0x08000000)
    else:
        import signal
        os.killpg(child.pid,signal.SIGTERM)

def run(root,jid,token):
    store=Store(root);j=store.get(jid);config=j['config'];attempt=Path(j['attempt']);meter=None;child=None;capture_thread=None;done=threading.Event()
    def heartbeat():
        while not done.wait(5):
            try:
                local=Store(root)
                with local.transaction():local.heartbeat(jid,token)
                local.db.close()
            except Exception:
                if meter:meter.stopped=True
                if child:stop_process(child)
                return
    watcher=threading.Thread(target=heartbeat,daemon=True);watcher.start()
    try:
        key=os.environ.get('DEEPSEEK_API_KEY','').strip()
        if not key:raise ValueError('DEEPSEEK_API_KEY is not available to the DSH process; no request sent')
        engine=config['engine'];cap=probe(engine)
        if config.get('expectedEngineVersion') and config['expectedEngineVersion'] not in cap['version']:raise ValueError('Engine version differs from configured pin')
        atomic(attempt/'engine.json',{'path':engine,'version':cap['version']})
        meter=Meter(key,config.get('model','deepseek-flash'),attempt/'api-usage.jsonl',config.get('requestLimit',100),config.get('qps',1),config.get('apiConcurrency',2))
        base=meter.start();out=attempt/'engine-output';out.mkdir()
        argv=[engine,j['source'],'--openai','--openai-base-url',base,'--openai-api-key',meter.token,
              '--openai-model',config.get('model','deepseek-flash'),'--output',str(out),'--lang-in','en','--lang-out','zh-CN',
              '--pages',','.join(map(str,j['sourcePages'])),'--translate-table-text']
        for flag,val in [('--qps',str(config.get('qps',1))),('--pool-max-worker',str(config.get('apiConcurrency',2)))]:
            if flag in cap['help']:argv.extend([flag,val])
        if '--no-dual' in cap['help']:argv.append('--no-dual')
        if '--ignore-cache' in cap['help']:argv.append('--ignore-cache')
        if config.get('glossary'):
            if '--glossaries' not in cap['help']:raise ValueError('Engine cannot accept required glossary')
            argv.extend(['--glossaries',config['glossary']])
        if config.get('prompt'):
            if '--custom-system-prompt' not in cap['help']:raise ValueError('Engine cannot accept academic translation prompt')
            argv.extend(['--custom-system-prompt',config['prompt']])
        env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        # The child only needs the short-lived loopback token; strip actual API credentials.
        for k in list(env):
            if k in ('DEEPSEEK_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'):env.pop(k,None)
        procopts={'creationflags':0x08000000} if os.name=='nt' else {'start_new_session':True}
        child=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,**procopts)
        atomic(attempt/'process.json',{'pid':child.pid,'startedAt':time.time()})
        def capture():
            with open(attempt/'engine.log','w',encoding='utf-8') as log:
                for data in iter(child.stdout.readline,b''):
                    line=data.decode('utf-8','replace').replace(key,'[REDACTED]').replace(meter.token,'[LOCAL-TOKEN]')
                    log.write(line);log.flush()
        capture_thread=threading.Thread(target=capture,daemon=True);capture_thread.start()
        try:code=child.wait(timeout=config.get('timeoutSeconds',3600))
        except subprocess.TimeoutExpired:stop_process(child);raise ValueError('Engine timed out; usage may include unreported in-flight requests')
        if code:raise ValueError(f'Engine exited {code}; inspect the redacted attempt log')
        if meter.stopped:raise ValueError('Job stopped')
        files=[p for p in out.rglob('*.pdf') if re.search(r'(^|[.\-_])mono([.\-_]|$)',p.name,re.I)]
        if len(files)!=1:raise ValueError('Cannot uniquely identify mono PDF; use stage with the explicit path/mapping')
        mono=files[0];info=pdf_info(mono)
        if info['pages']==j['sourceInfo']['pages'] and len(j['sourcePages'])!=info['pages']:
            import pymupdf as fitz
            doc=fitz.open(mono);selected=fitz.open()
            for number in j['sourcePages']:selected.insert_pdf(doc,from_page=number-1,to_page=number-1)
            mono=attempt/'selected-mono.pdf';selected.save(mono);doc.close();selected.close()
        with store.transaction():store.stage(jid,{'mono':str(mono)},j['producers'][-1],token=token)
    except Exception as e:
        with store.transaction():store.failure(jid,token,str(e))
    finally:
        done.set()
        if child and child.poll() is None:stop_process(child)
        if child:
            child.wait(timeout=30)
            if capture_thread:capture_thread.join(timeout=10)
            if child.stdout:child.stdout.close()
        if meter:meter.close()
        store.db.close()

if __name__=='__main__':run(sys.argv[1],sys.argv[2],sys.argv[3])
