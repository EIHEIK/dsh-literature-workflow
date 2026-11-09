"""Real isolated composition/review transport, synthetic responses, zero API calls."""
import json
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
import pymupdf as fitz
from batches import BatchStore
from broker_pump import BrokerPump
from controller import Controller
from corpus import CorpusIndex
from core import CHECKS,atomic,digest,read_json
from docker_runner import DockerRunner
from model_broker import ModelBroker,PriceBook
from stage_handlers import ComposeHandler,ReviewHandler,RepairHandler,artifact


def main():
    delivery_mode='--delivery' in sys.argv
    limit_mode='--repair-limit' in sys.argv
    early_mode='--early-negative' in sys.argv
    repair_mode='--repair' in sys.argv or delivery_mode or limit_mode
    case=ROOT/'.test-output'/(('docker-repair-' if repair_mode else 'docker-review-')+time.strftime('%Y%m%d-%H%M%S'));case.mkdir(parents=True)
    source=case/'source.pdf';mono=case/'mono.pdf'
    for path,chinese in ((source,False),(mono,True)):
        doc=fitz.open();page=doc.new_page()
        if chinese:page.insert_text((60,60),'气动力是标量。速度为 21 m/s。' if repair_mode else '气动力是一个矢量。本页仅用于离线测试。',fontname='china-s',fontsize=3.2 if repair_mode else 11)
        else:page.insert_text((60,60),'Aerodynamic force is a vector. Speed is 20 m/s. Offline fixture.' if repair_mode else 'Aerodynamic force is a vector. Offline fixture.',fontsize=11)
        if early_mode:
            page=doc.new_page()
            if chinese:page.insert_text((60,60),'气动力是矢量。速度为 20 m/s。',fontname='china-s',fontsize=11)
            else:page.insert_text((60,60),'Aerodynamic force is a vector. Speed is 20 m/s. Offline fixture.',fontsize=11)
        doc.save(path);doc.close()
    mapping=case/'mapping.json';atomic(mapping,{'sourcePages':[1,2] if early_mode else [1]})
    store=BatchStore(case/'data');item={'source':str(source),'title':'OFFLINE REVIEW FIXTURE'}
    if delivery_mode:item['zotero']={'libraryType':'user','parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP'}
    summary=store.prepare([item],{'revision':'review-fixture'})
    store.confirm(summary['batch'],summary['summaryHash'],'synthetic-offline-fixture');store.start(summary['batch'],summary['summaryHash'])
    job=summary['items'][0]['job']
    job_folder=store.folder(store.get(job));shutil.copyfile(mono,job_folder/'fixture-mono.pdf');shutil.copyfile(mapping,job_folder/'fixture-mapping.json')
    mono=job_folder/'fixture-mono.pdf';mapping=job_folder/'fixture-mapping.json';source=Path(store.get(job)['source'])
    corpus=CorpusIndex(store.db)
    with fitz.open(source) as document:source_text=document[0].get_text(sort=True)
    with store.transaction():
        registered=corpus.add_source({'kind':'paper','job':job,'sha256':digest(source),'title':'OFFLINE FIXTURE','retrievedAt':1},
                                    [{'page':1,'anchor':'page-1-text','text':source_text}])
        term_id=corpus.add_translation('term','Aerodynamic force','气动力','aerodynamics',registered['passages'][0],producer_job=job)
    registration=job_folder/'fixture-corpus.json';atomic(registration,{'source':registered['source'],'passages':registered['passages'],'candidateTermIds':[term_id]})
    background=job_folder/'fixture-background.json';atomic(background,{'content':{'domain':'aerodynamics'}})
    # Explicit fixture staging; neither background nor translation quality is claimed.
    for artifacts in ({'fixture':artifact(source),'corpusRegistration':artifact(registration),'background':artifact(background)},
                      {'mono':artifact(mono),'mapping':artifact(mapping)}):
        phase=store.claim_next();store.finish(job,phase['token'],artifacts)
    broker=ModelBroker(store,PriceBook(read_json(ROOT/'deploy/deepseek-pricing.json')));calls=[];review_coverage={}
    def executor(payload):
        content=payload['messages'][1]['content']
        context=json.loads(content if isinstance(content,str) else content[0]['text'])
        if 'blocks' in context:
            if not repair_mode:raise ValueError('Unexpected repair request')
            if sum(p['type']=='image_url' for p in content)!=3:raise ValueError('Missing actual source images for repair')
            editable=[b for b in context['blocks'] if not b['protected']]
            block=next((b for b in editable if '标量' in b['text'] or '21m/s' in ''.join(b['text'].split())),
                       next(b for b in editable if '气动力' in b['text']))
            report={'candidateId':context['candidateId'],'sourcePage':context['sourcePage'],
                    'updates':[{'blockId':block['id'],'originalSha256':block['sha256'],
                        'sourceQuote':context['sourceText'],'replacement':'气动力是矢量。速度为 20 m/s。本页仅用于离线测试。',
                        'reason':'SYNTHETIC correction of scalar/vector and numeric error'}],
                    'supplements':[],'unresolved':[]}
            if context['repairRound']>1:report.update(updates=[],unresolved=['SYNTHETIC issue deliberately left unresolved'])
            calls.append('repair-'+str(context['sourcePage']))
        else:
            review_coverage.setdefault(context['candidateId'],set()).add(context['unit']['id'])
            if sum(p['type']=='image_url' for p in content)!=7:raise ValueError('Missing review images')
            report={'unitId':context['unit']['id'],'candidateId':context['candidateId'],
                'checks':{k:{'passed':True,'evidence':'SYNTHETIC FIXTURE ONLY; not an actual quality review'} for k in CHECKS},
                'issues':[],'verdict':'pass','summary':'SYNTHETIC FIXTURE ONLY'}
            translated=''.join(''.join(t['text'].split()) for t in context['unit']['sourceTranslations'])
            report['corpusReview']=[{'id':t['id'],'verdict':'verified' if ''.join(t['target'].split()) in translated else 'rejected',
                                    'evidence':'SYNTHETIC explicit corpus identity check'} for t in context['unit']['corpusCandidates']]
            if repair_mode and ('标量' in translated or '21m/s' in translated):
                report['checks']['faithfulness']['passed']=False
                report.update(verdict='needs_attention',issues=[{'id':'semantic-fixture','severity':'major','status':'open',
                    'detail':'SYNTHETIC scalar/vector and 21/20 m/s discrepancy remains'}])
            if limit_mode:
                report['checks']['faithfulness']['passed']=False
                report.update(verdict='needs_attention',issues=[{'id':'forced','severity':'major','status':'open',
                              'detail':'SYNTHETIC forced unresolved issue to test the repair limit'}])
            calls.append(context['unit']['id'])
        return {'kind':'response','body':{'id':'offline-review-'+str(len(calls)),'model':'deepseek-flash',
            'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110},
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(report,ensure_ascii=False)}}]}}
    pump=BrokerPump(broker,executor)
    runner=DockerRunner(store.root,ROOT/'runtime',ROOT/'deploy/engine-image.lock.json',{'compose':ComposeHandler(),'review':ReviewHandler(),'repair':RepairHandler()})
    controlled=runner
    if delivery_mode:
        from delivery_runner import DeliveryRunner
        sys.path.insert(0,str(ROOT/'tests'))
        from test_delivery_runner import FixtureZotero
        fake_zotero=FixtureZotero(case,source);fake_zotero.lose_second_response=True
        controlled=DeliveryRunner(runner,zotero_factory=lambda:fake_zotero)
    controlled.bind(store);controller=Controller(store.root,controlled,min_free_bytes=0);controller.broker_pump=pump
    deadline=time.monotonic()+100;report=None
    try:
        while time.monotonic()<deadline:
            controller.tick(store);phase=store.phase(job)
            if phase['stage']=='accept' and report is None:
                current=store.get(job)
                if current['acceptance'] or current['publication'] or current['archive']:raise ValueError('Fixture must not publish or archive')
                if store.db.execute('SELECT COUNT(*) FROM corpus_trust').fetchone()[0]:raise ValueError('Corpus promoted before acceptance')
                # Verify reconstructibility without changing job to accepted.
                evidence=store.acceptance(current)
                # Simulate the crash window after stage output registration but
                # before the controller persisted its checkpoint.
                from candidate_ops import register_composition
                compose_phase={**phase,'stage':'compose','generation':current['candidate']['compositionOrigin']['generation']}
                record=read_json(runner.record_path(compose_phase))
                before=current['candidate']['id']
                replay=register_composition(store,compose_phase,record,read_json(Path(record['work'])/'result.json'))
                if store.get(job)['candidate']['id']!=before or not replay['artifacts']:raise ValueError('Candidate replay duplicated registration')
                report={'passed':True,'modelCalls':0,'syntheticExecutorCalls':len(calls),'image':runner.image,
                        'nextStage':'accept','accepted':False,'published':False,'zoteroWrites':0,
                        'candidate':current['candidate']['id'],'review':evidence['review'],'candidateReplayStable':True,
                        'repairRounds':phase['repairRound'],'calls':calls,'cnPages':current['candidate']['layout']['cnPages'],
                        'boundary':'Real network-none compose/review containers and private receipt reconstruction; responses synthetic, no quality claim'}
                if repair_mode and (phase['repairRound']!=1 or calls.count('repair-1')!=1 or len(review_coverage)!=2 or
                    len(review_coverage[current['candidate']['id']])!=current['candidate']['layout']['cnPages']):
                    raise ValueError('Expected one repair and complete visual coverage of the final candidate')
                if repair_mode:
                    texts=read_json(current['candidate']['files']['reviewText']['path'])['cn']
                    combined=''.join(texts.values())
                    compact=''.join(combined.split())
                    if '21m/s' in compact or '20m/s' not in compact or '标量' in compact:raise ValueError('Semantic fixture repair did not apply')
                    report['semanticFixtureCorrection']=True
                if early_mode:
                    reviews=[read_json(json.loads(row[0])['report']['path']) for row in store.db.execute('SELECT body FROM tickets') if json.loads(row[0]).get('report')]
                    partial=[r for r in reviews if r.get('reviewCoverage',{}).get('complete') is False]
                    if (len(partial)!=1 or partial[0]['verdict']!='needs_attention' or partial[0]['checks']['complete_scope'] is not False or
                        partial[0]['reviewCoverage']['reviewedUnits']>=partial[0]['reviewCoverage']['requiredUnits']):
                        raise ValueError('Expected a genuine incomplete negative report before repair')
                    final=next(r for r in reviews if r['candidateId']==current['candidate']['id'])
                    if final['verdict']!='pass' or final['reviewCoverage']['complete'] is not True:raise ValueError('Full current review required')
                    report['initialNegativeCoverage']=partial[0]['reviewCoverage'];report['finalCoverage']=final['reviewCoverage']
                if not delivery_mode:break
            if delivery_mode and phase['state']=='done':
                current=store.get(job)
                if current['state']!='archived' or fake_zotero.imports!=2 or set(current['archive'])!={'cn','bi'}:
                    raise ValueError('Terminal delivery failed fixture reconciliation')
                report.update(nextStage='done',fixtureAccepted=True,fixturePublished=True,fixtureArchived=True,
                              syntheticZoteroImports=fake_zotero.imports,lostImportResponseRecovered=True,
                              boundary='Real isolated compose/review/repair and host delivery lifecycle; provider and Zotero simulated, no production quality claim')
                trusted=corpus.trusted_translations(store,'aerodynamics')
                if {t['kind'] for t in trusted}!={'term','memory'}:raise ValueError('Expected explicit term and memory promotion')
                report['trustedFixtureKinds']=sorted({t['kind'] for t in trusted});report['trustedFixtureCount']=len(trusted)
                break
            if phase['state']=='blocked':
                if not limit_mode:raise RuntimeError(str(phase['error']))
                if phase['stage']!='review' or phase['repairRound']!=2:raise ValueError('Unexpected repair-limit blocker')
                controller.tick(store);phase=store.phase(job);package=phase.get('problemPackage')
                if not package or digest(package['path'])!=package['sha256']:raise ValueError('Missing problem package')
                if store.get(job)['acceptance'] or store.get(job)['publication']:raise ValueError('Failed fixture was accepted')
                if sum(c.startswith('repair-') for c in calls)!=2:raise ValueError('Incorrect repair count')
                report={'passed':True,'modelCalls':0,'zoteroWrites':0,'repairRounds':2,'calls':calls,
                        'blockedStage':phase['stage'],'problemPackage':package,'accepted':False,'published':False,
                        'boundary':'Actual isolated workers; forced synthetic review failure stops after two repair rounds and creates local issue bundle'}
                break
            time.sleep(.2)
        if report is None or (delivery_mode and phase['state']!='done'):raise TimeoutError('Review fixture timed out')
        atomic(case/'validation.json',report);print(json.dumps({'evidence':str(case/'validation.json'),'report':report},ensure_ascii=False))
    finally:
        pump.close();phase=store.phase(job)
        if phase.get('worker'):controlled.cancel(phase)
        store.db.close()


if __name__=='__main__':main()
