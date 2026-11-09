"""Long-running local controller entrypoint; no chat needs to stay open.

New deployments are offline by default. Provisioning never writes an API key.
Production enablement requires a deployment readiness receipt for this runtime.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.request

from broker_pump import BrokerPump
from controller import Controller
from control_api import ControlAPI
from core import atomic,code_hash,digest,read_json
from delivery_runner import DeliveryRunner
from deployment_policy import DeploymentPolicy
from docker_runner import DockerRunner
from local_service import LocalService
from library_index import PDFScanner
from index_refresh import IndexRefresh
from model_broker import ModelBroker,PriceBook
from model_gate import ModelGate
from provider_http import DeepSeekHTTP,NoRedirect
from stage_handlers import BackgroundHandler,TranslateHandler,ComposeHandler,ReviewHandler,RepairHandler
from zotero_resolver import ZoteroResolver


def provision(path,data,runtime):
    path=Path(path).resolve();runtime=Path(runtime).resolve(strict=True);data=Path(data).resolve()
    if path.exists():raise ValueError('Existing service configuration is preserved; use its path')
    config={'schema':1,'data':str(data),'runtime':str(runtime),'imageLock':str(runtime.parent/'deploy/engine-image.lock.json'),
            'priceBook':str(runtime.parent/'deploy/deepseek-pricing.json'),'port':0,'paidWorkEnabled':False,'supervisionMode':'codex',
            'readinessReceipt':str(path.parent/'readiness.json'),'sessionsRoot':str(Path.home()/'.codex/sessions'),
            'pilotReadinessReceipt':str(path.parent/'pilot-readiness.json'),
            'proxy':'http://127.0.0.1:7890','credentials':{role:secrets.token_urlsafe(40) for role in ('dsh','dot','codex','confirmation','maintenance')},
            'jobConfig':{'revision':'background-service-v2','stageTimeoutSeconds':3600,'progressTimeoutSeconds':900}}
    atomic(path,config)
    return {'config':str(path),'paidWorkEnabled':False,'credentialsWritten':True,'apiKeyStored':False}


def readiness(config):
    return DeploymentPolicy(config).production_ready()


def export_adapters(path):
    """Trusted installation step: each adapter receives only its required roles."""
    path=Path(path).resolve(strict=True);config=read_json(path)
    if type(config.get('port')) is not int or not 1<=config['port']<=65535:
        raise ValueError('Start the service once to provision its fixed local port')
    url='http://127.0.0.1:'+str(config['port'])
    dsh=path.parent/'dsh-adapter.json';dot=path.parent/'dot-adapter.json'
    atomic(dsh,{'url':url,'dshToken':config['credentials']['dsh']})
    atomic(dot,{'url':url,'dotToken':config['credentials']['dot'],
                'confirmationToken':config['credentials']['confirmation'],'sessionsRoot':config['sessionsRoot']})
    codex=path.parent/'codex-adapter.json'
    if 'codex' in config['credentials']:
        atomic(codex,{'url':url,'codexToken':config['credentials']['codex'],'sessionsRoot':config['sessionsRoot']})
    return {'dshConfig':str(dsh),'dotConfig':str(dot),'codexConfig':str(codex) if codex.exists() else None,'apiKeyStored':False,'maintenanceCredentialExported':False}


def run(path):
    path=Path(path).resolve(strict=True);config=read_json(path)
    if Path(config['runtime']).resolve()!=Path(__file__).resolve().parent:raise ValueError('Service runtime path mismatch')
    if config.get('deploymentReceipt'):
        from deployment_install import verify_installation
        verify_installation(path)
    data=Path(config['data']);data.mkdir(parents=True,exist_ok=True)
    handlers={'background':BackgroundHandler(),'translate':TranslateHandler(),'compose':ComposeHandler(),
              'review':ReviewHandler(),'repair':RepairHandler()}
    runner=DeliveryRunner(DockerRunner(data,config['runtime'],config['imageLock'],handlers))
    policy=DeploymentPolicy(config);can_run=policy.enabled()
    def broker_factory(store):
        return BrokerPump(ModelBroker(store,PriceBook(read_json(config['priceBook']))),
                          DeepSeekHTTP(os.environ.get('DEEPSEEK_API_KEY',''),proxy=config.get('proxy')))
    def startup(store):
        IndexRefresh.initialize(store)
        policy.guard(store)
    controller=Controller(data,runner,broker_factory=broker_factory if can_run else None,startup_hook=startup,dispatch_guard=policy.guard)
    thread=threading.Thread(target=controller.run,name='literature-controller');thread.start()
    service=None;indexer=None;state=None;state_path=path.parent/'service-state.json'
    try:
        if not controller.ready.wait(10) or controller.error:raise RuntimeError('Controller did not acquire its runtime')
        resolver=ZoteroResolver(data/'library-scan')
        indexer=IndexRefresh(controller,resolver,PDFScanner(data/'library-scan',config['runtime'],config['imageLock']))
        indexer.start()
        api=ControlAPI(resolver,config['jobConfig'],pilot=not policy.production_ready(),readiness=lambda:can_run and policy.production_ready(),
                       pilot_readiness=lambda:can_run and policy.pilot_ready(),scoped_readiness=lambda:can_run and policy.scoped_batch_ready(),batch_authorizer=lambda store,batch:can_run and policy.allows(store,batch),index_refresh=indexer,
                       confirmation_method='recorded-user-instruction' if config.get('supervisionMode')=='codex' else 'trusted-host-elicitation')
        gate=ModelGate(config['sessionsRoot'])
        codex_gate=ModelGate(config['sessionsRoot'],expected_effort=None)
        service=LocalService(controller,api,config['credentials'],dot_gate=gate.check if config.get('supervisionMode')=='dot' else None,
                             codex_gate=codex_gate.check if config.get('supervisionMode')=='codex' else None,port=config['port']);service.start()
        # The first OS-assigned loopback port becomes stable across restarts.
        # A later conflict fails closed; adapters must not silently move targets.
        if config['port']==0:
            config['port']=service.server.server_port;atomic(path,config)
        state={'pid':os.getpid(),'url':service.url,'startedAt':time.time(),
               'runtimeHash':code_hash(),'productionReady':policy.production_ready(),'pilotReady':policy.pilot_ready(),'scopedBatchReady':policy.scoped_batch_ready(),'state':'running'}
        atomic(state_path,state);print(json.dumps({k:v for k,v in state.items() if k!='pid'}),flush=True)
        while thread.is_alive():thread.join(timeout=.5)
    except KeyboardInterrupt:controller.stop()
    finally:
        if indexer:indexer.close()
        controller.stop();thread.join(timeout=10)
        if thread.is_alive():
            if state:atomic(state_path,{**state,'state':'stopping','error':controller.error})
            while thread.is_alive():thread.join(timeout=.5)
        if service:service.close()
        if state:atomic(state_path,{**state,'state':'stopped','stoppedAt':time.time(),'error':controller.error})


def local_call(config_path,stop=False):
    path=Path(config_path);config=read_json(path);state=read_json(path.parent/'service-state.json')
    role='maintenance' if stop else 'dsh';endpoint='/maintenance/stop' if stop else '/rpc'
    payload={'action':'stop'} if stop else {'method':'get_capabilities','arguments':{}}
    request=urllib.request.Request(state['url']+endpoint,json.dumps(payload).encode(),
        {'Content-Type':'application/json','Authorization':'Bearer '+config['credentials'][role]})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=10) as response:return json.load(response)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['provision','run','status','stop','export-adapters']);parser.add_argument('--config',required=True)
    parser.add_argument('--data');args=parser.parse_args()
    if args.action=='provision':
        if not args.data:parser.error('--data is required for provisioning')
        print(json.dumps(provision(args.config,args.data,Path(__file__).parent),ensure_ascii=False))
    elif args.action=='run':run(args.config)
    elif args.action=='export-adapters':print(json.dumps(export_adapters(args.config),ensure_ascii=False))
    else:print(json.dumps(local_call(args.config,args.action=='stop'),ensure_ascii=False))
