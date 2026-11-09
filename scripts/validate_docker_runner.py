"""Real controller/runner/broker roundtrip with a fake paid-model executor.

Uses the pinned production engine image; never reads an API key or calls a model.
No fixture marks a translation, review, publication or Zotero archive accepted.
"""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from batches import BatchStore
from broker_pump import BrokerPump
from controller import Controller
from core import atomic,digest,object_hash,read_json
from docker_runner import DockerRunner
from model_broker import ModelBroker,PriceBook
from stage_handlers import BackgroundHandler,TranslateHandler


class TransportHandler:
    def prepare(self,store,phase,job):
        return {'settings':{'mode':'transport_probe','offlineFixture':True},
                'policy':{'model':'deepseek-flash','thinking':'disabled','maxOutputTokens':4096}}
    def finish(self,store,phase,record,result):
        rows=store.db.execute("SELECT body FROM model_calls WHERE job_id=? AND state='completed'",(phase['job'],)).fetchall()
        if len(rows)!=1:raise ValueError('Fixture requires exactly one private broker receipt')
        call=json.loads(rows[0][0]);receipt=read_json(call['response']['path'])
        if result['responseHash']!=object_hash(receipt) or receipt['id']!='offline-runner-fixture':
            raise ValueError('Untrusted worker reply differs from private broker receipt')
        path=Path(record['work'])/'provider-response.json'
        if digest(path)!=call['response']['sha256']:raise ValueError('Worker response bytes changed')
        return {'artifacts':{'transportFixture':{'path':str(path),'sha256':digest(path)}}}


engine_probe='--engine-probe' in sys.argv
restart_probe='--restart' in sys.argv
background_mode='--background' in sys.argv or engine_probe or restart_probe
case=ROOT/'.test-output'/('docker-'+('background' if background_mode else 'runner')+'-'+time.strftime('%Y%m%d-%H%M%S'));case.mkdir(parents=True)
import pymupdf as fitz
source=case/'fixture.pdf';doc=fitz.open();p=doc.new_page();p.insert_text((60,60),'Aerodynamic force is a vector. Offline fixture.');doc.save(source);doc.close()
store=BatchStore(case/'data')
summary=store.prepare([{'source':str(source),'title':'Offline runner fixture'}],{'revision':'runner-fixture'})
store.confirm(summary['batch'],summary['summaryHash'],'synthetic-offline-fixture')
store.start(summary['batch'],summary['summaryHash'])
book=PriceBook(read_json(ROOT/'deploy/deepseek-pricing.json'));broker=ModelBroker(store,book)
count=[0];engine_requests=[]
def executor(payload):
    if engine_probe and payload['thinking']['type']=='disabled':
        engine_requests.append({'keys':sorted(payload),'messages':len(payload['messages']),
                                'model':payload['model'],'maxOutputTokens':payload['max_tokens']})
        return {'kind':'unknown','reason':'offline_probe_stopped_before_provider','retryable':False}
    if background_mode:
        if 'Aerodynamic force is a vector.' not in payload['messages'][-1]['content']:raise ValueError('Missing source in background fixture')
        content=json.dumps({'domain':'aerodynamics','summary':'离线背景夹具','abbreviations':[],
            'candidateTerms':[{'source':'Aerodynamic force','target':'气动力','sourcePage':1,'sourceQuote':'Aerodynamic force is a vector.'}],
            'limitations':['fixture_only']},ensure_ascii=False)
    else:
        if payload['messages'][0]['content']!='OFFLINE FIXTURE: controller to isolated production image transport':raise ValueError('Unexpected fixture')
        content='离线容器夹具回复'
    count[0]+=1
    return {'kind':'response','body':{'id':'offline-runner-fixture','model':'deepseek-flash',
        'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110},
        'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}]}}
pump=BrokerPump(broker,executor)
handlers={'background':BackgroundHandler() if background_mode else TransportHandler()}
if engine_probe:handlers['translate']=TranslateHandler()
runner=DockerRunner(store.root,ROOT/'runtime',ROOT/'deploy/engine-image.lock.json',handlers)
runner.bind(store);controller=Controller(store.root,runner,min_free_bytes=0);controller.broker_pump=pump
restarted_worker=None
if restart_probe:
    controller.tick(store)
    restarted_worker=store.phase(summary['items'][0]['job'])['worker']
    pump.close();root=store.root;store.db.close()
    store=BatchStore(root);broker=ModelBroker(store,book);broker.recover_controller_restart()
    pump=BrokerPump(broker,executor)
    runner=DockerRunner(root,ROOT/'runtime',ROOT/'deploy/engine-image.lock.json',handlers);runner.bind(store)
    controller=Controller(root,runner,min_free_bytes=0);controller.broker_pump=pump
deadline=time.monotonic()+100;job=summary['items'][0]['job'];report=None
try:
    while time.monotonic()<deadline:
        controller.tick(store)
        phase=store.phase(job)
        if engine_probe and engine_requests:
            store.control(summary['batch'],'cancel');controller.tick(store)
            report={'passed':True,'image':runner.image,'executorCalls':count[0],'modelCalls':0,
                    'syntheticSpentMicros':store.budgets.status(job)['spent'],'engineRequestObserved':engine_requests[0],
                    'boundary':'Actual pinned pdf2zh engine emitted a broker-valid request from a network-none worker; intentionally stopped before provider/translation'}
            break
        if phase['stage']=='translate' and not engine_probe:
            report={'passed':True,'image':runner.image,'executorCalls':count[0],'modelCalls':0,
                    'syntheticSpentMicros':store.budgets.status(job)['spent'],
                    'nextStage':phase['stage'],'checkpoint':phase['lastCheckpoint'],
                    'boundary':'Controller + Docker runner + private broker + mailbox; no production API or translation quality validation'}
            if background_mode:
                report['candidateTerms']=store.db.execute('SELECT COUNT(*) FROM corpus_translations').fetchone()[0]
                report['trustedTerms']=store.db.execute('SELECT COUNT(*) FROM corpus_trust').fetchone()[0]
                if report['candidateTerms']!=1 or report['trustedTerms']!=0:raise ValueError('Background trust boundary failed')
            if restart_probe:
                actual=runner.docker(['ps','--all','--filter','label=literature.job='+job,'--format','{{.Names}}'])
                names=actual.stdout.splitlines()
                if actual.returncode or names!=[restarted_worker]:raise ValueError('Restart duplicated or lost the actual worker')
                report['controllerRestartReattached']=True;report['actualWorkerCount']=len(names)
            break
        if phase['state']=='blocked':raise RuntimeError('Fixture blocked: '+str(phase['error']))
        time.sleep(.15)
    if report is None:raise TimeoutError('Runner fixture timed out')
finally:
    pump.close()
    phase=store.phase(job)
    if phase.get('worker'):runner.cancel(phase)
    store.db.close()
if report['executorCalls']!=1 or report['syntheticSpentMicros']!=280:raise ValueError('Unexpected fixture ledger')
atomic(case/'validation.json',report)
print(json.dumps({'evidence':str(case/'validation.json'),'report':report},ensure_ascii=False))
