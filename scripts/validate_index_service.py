"""Actual unpaid local-service refresh of an already reconciled Zotero intake."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,read_json
from provider_http import NoRedirect
from service_main import local_call,export_adapters


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--intake',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();config_path=Path(args.config);config=read_json(config_path);output=Path(args.output)
    if config.get('paidWorkEnabled'):raise ValueError('Only offline service validation is allowed')
    state_path=config_path.parent/'service-state.json'
    if state_path.exists() and read_json(state_path).get('state')!='stopped':raise ValueError('Reconcile existing service before validation')
    expected=read_json(args.intake)['ready'];scope={'kind':'items','keys':sorted({x['zotero_key'] for x in expected})}
    request={'scope':scope,'requestId':'real-index-'+str(time.time_ns())}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def rpc(method,arguments):
        state=read_json(state_path)
        req=urllib.request.Request(state['url']+'/rpc',json.dumps({'method':method,'arguments':arguments}).encode(),
            {'Content-Type':'application/json','Authorization':'Bearer '+config['credentials']['dsh']})
        with opener.open(req,timeout=35) as response:return json.load(response)['result']
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.with_suffix('.log').open('wb') as log:
        started=time.time();process=subprocess.Popen([sys.executable,str(ROOT/'runtime/service_main.py'),'run','--config',str(config_path)],
            cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=0x08000000)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('Service exited before ready')
                if state_path.exists():
                    state=read_json(state_path)
                    if state.get('state')=='running' and state['startedAt']>=started:break
                time.sleep(.1)
            else:raise TimeoutError('Service startup not observed')
            capabilities=rpc('get_capabilities',{})
            if capabilities['productionReady'] or not capabilities['indexRefreshAvailable']:raise ValueError('Wrong service readiness')
            adapters=export_adapters(config_path)
            node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
            checked=subprocess.run([str(node),'--input-type=module','-e',
                "import {serviceCall} from './lib/service-client.js'; const value=await serviceCall(process.argv[1],'get_capabilities',{}); if(value.productionReady||!value.indexRefreshAvailable)process.exit(2); console.log('DSH adapter connected to offline controller');",
                adapters['dshConfig']],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=45,creationflags=0x08000000)
            if checked.returncode:raise ValueError('Actual Node DSH adapter failed to connect')
            first=rpc('refresh_index',request);again=rpc('refresh_index',request)
            if first['index']!=again['index']:raise ValueError('Repeated request created another index job')
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                result=rpc('get_index_status',{'index':first['index']})
                atomic(output.with_name(output.stem+'-progress.json'),result)
                if result['state'] not in ('queued','running'):break
                time.sleep(.5)
            else:raise TimeoutError('Index continues in its persisted task; inspect before retry')
            if result['state']!='completed':raise ValueError('Index refresh has unresolved sources')
            actual={x['parentItemKey']:x['result']['sourceSha256'] for x in result['inventory']}
            if actual!={x['zotero_key']:x['sourceSha256'] for x in expected}:raise ValueError('Reconciled intake changed')
            report={'passed':True,'index':first['index'],'papers':len(actual),'pages':sum(x['result']['pages'] for x in result['inventory']),
                    'idempotentRequest':True,'sourceHashesMatch':True,'dshNodeAdapterConnected':True,'productionReady':False,'modelCalls':0,'zoteroWrites':0,
                    'boundary':'Actual detached service, authenticated HTTP and current Zotero; existing verified offline extraction cache reused'}
        finally:
            if process.poll() is None:local_call(config_path,stop=True);process.wait(timeout=20)
        if process.returncode or read_json(state_path)['state']!='stopped':raise ValueError('Service did not stop cleanly')
        report['cleanShutdown']=True;atomic(output,report);print(json.dumps(report))


if __name__=='__main__':main()
