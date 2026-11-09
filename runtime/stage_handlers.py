"""Trusted phase adapters. Worker JSON is data; private broker receipts own proof."""
import json
from pathlib import Path
import time

from api_mailbox import read_bounded
from core import atomic,digest,object_hash,read_json
from corpus import CorpusIndex


def artifact(path):return {'path':str(path),'sha256':digest(path)}


class MissingArtifact(ValueError):pass


def delivered_responses(store,phase):
    rows=store.db.execute('''SELECT DISTINCT c.body FROM model_deliveries d JOIN model_calls c ON c.id=d.call_id
        WHERE d.job_id=? AND d.stage=? AND d.generation=?''',(phase['job'],phase['stage'],phase['generation'])).fetchall()
    result=[]
    private=(store.root/'broker-private').resolve()
    for raw, in rows:
        call=json.loads(raw);receipt=call.get('response') or {};path=Path(receipt.get('path','')).resolve(strict=True)
        if call['state']!='completed' or call['job']!=phase['job'] or not path.is_relative_to(private) or digest(path)!=receipt['sha256']:
            raise ValueError('Private API receipt is missing or changed')
        result.append({'call':call['id'],'body':read_json(path),'receipt':receipt})
    if not result:raise ValueError('No verified provider delivery for this stage generation')
    return result


def previous_artifact(store,phase,name):
    cursor=phase.get('lastCheckpoint')
    while cursor:
        row=store.db.execute('SELECT body FROM phase_checkpoints WHERE id=?',(cursor,)).fetchone()
        if row is None:raise ValueError('Missing checkpoint')
        checkpoint=json.loads(row[0])
        if name in checkpoint['artifacts']:
            value=checkpoint['artifacts'][name]
            if digest(value['path'])!=value['sha256']:raise ValueError('Changed stage input')
            return Path(value['path'])
        cursor=checkpoint['previous']
    raise MissingArtifact('Required upstream artifact unavailable: '+name)


class BackgroundHandler:
    def prepare(self,store,phase,job):
        corpus=CorpusIndex(store.db)
        # The local corpus can be populated by the separately sandboxed library
        # extraction adapter. Missing coverage is explicit, not invented context.
        matches=corpus.search(job['title'],limit=100)
        sources=set();web_sources=set();references=[];chars=0
        for item in matches:
            ref=item['reference'];kind=ref['kind']
            if kind not in ('zotero','web') or ref['sha256']==job['sourceSha256']:continue
            # A newly indexed corrected source supersedes only its retrieval
            # eligibility; old immutable passages and their receipts remain.
            if kind=='zotero' and ref.get('extractorVersion')!=__import__('source_text').EXTRACTOR_VERSION:continue
            pool,cap=(sources,10) if kind=='zotero' else (web_sources,5)
            if ref['sha256'] not in pool and len(pool)>=cap:continue
            if chars+len(item['text'])>30000:continue
            references.append(item);pool.add(ref['sha256']);chars+=len(item['text'])
        path=store.folder(job)/'background-inputs'/f"references-{phase['generation']}.json"
        atomic(path,{'references':references,'sourceCount':len(sources),'coverage':'indexed_references' if references else 'own_paper_only',
                     'webSourceCount':len(web_sources),'webRetrieval':'cached_verified_sources_only','trust':'reference_only'})
        return {'settings':{'mode':'background'},'inputs':{'/input/references.json':path},
                'policy':{'model':'deepseek-flash','thinking':'enabled','maxOutputTokens':32768,'jsonOutput':True}}

    def finish(self,store,phase,record,result):
        responses=delivered_responses(store,phase)
        match=[r for r in responses if r['body'].get('id')==result.get('providerId') and object_hash(r['body'])==result.get('responseHash')]
        if len(match)!=1:raise ValueError('Background response has no unique trusted provider receipt')
        folder=Path(record['work']);background=read_bounded(folder/'background.json',folder,1024*1024)
        content=json.loads(match[0]['body']['choices'][0]['message']['content'])
        if background.get('content')!=content or background.get('sourceSha256')!=record['sourceSha256']:
            raise ValueError('Background differs from API response or source')
        pages=read_bounded(folder/'source-pages.json',folder,16*1024*1024)
        job=store.get(phase['job'])
        if pages.get('sourceSha256')!=job['sourceSha256'] or [p['sourcePage'] for p in pages['pages']]!=list(range(1,job['sourceInfo']['pages']+1)):
            raise ValueError('Background source coverage mismatch')
        passages=[{'page':p['sourcePage'],'anchor':f'page-{p["sourcePage"]}-text','text':p['text'][:30000]} for p in pages['pages'] if p['text'].strip()]
        index=CorpusIndex(store.db)
        with store.transaction():
            indexed=index.add_source({'kind':'paper','job':job['id'],'sha256':job['sourceSha256'],
                                      'title':job['title'],'retrievedAt':job['createdAt']},passages)
            # Only verbatim, located candidates enter the shared corpus. Invalid
            # proposed terms are kept in the background artifact, not promoted.
            candidates=[];terms=content.get('candidateTerms',[]);domain=content.get('domain','')
            if not isinstance(terms,list) or len(terms)>200 or not isinstance(domain,str) or not 1<=len(domain)<=200:
                raise ValueError('Invalid background term schema')
            for term in terms:
                if not isinstance(term,dict):continue
                page=term.get('sourcePage');quote=term.get('sourceQuote');source=term.get('source');target=term.get('target')
                if not all(isinstance(v,str) and v.strip() for v in (quote,source,target)) or type(page) is not int:continue
                for pid in indexed['passages']:
                    passage=index.passage(pid)
                    if passage['page']==page and quote in passage['text'] and source in quote:
                        candidates.append(index.add_translation('term',source,target,domain,pid,producer_job=job['id']));break
            pack=index.freeze_pack(store,job['id'],indexed['passages'],job['title'],domain)
            atomic(folder/'background-pack.json',pack)
        evidence=folder/'corpus-registration.json'
        atomic(evidence,{'source':indexed['source'],'passages':indexed['passages'],'candidateTermIds':candidates,
                         'trustedTermIds':[],'providerCall':match[0]['call']})
        return {'artifacts':{'background':artifact(folder/'background.json'),'sourcePages':artifact(folder/'source-pages.json'),
                             'corpusRegistration':artifact(evidence),'backgroundPack':artifact(folder/'background-pack.json')}}


class TranslateHandler:
    def prepare(self,store,phase,job):
        imported=job['config'].get('importTranslation')
        if imported:
            from repaired_import import validate_import,EXTRAS
            rounds=validate_import(imported,job['sourceSha256'],job['sourcePages'])
            if digest(imported['path'])!=imported['sha256'] or set(imported['mapping'])!=set(job['sourcePages']):
                raise ValueError('Frozen legacy translation changed or scope differs')
            inputs={'/input/legacy.pdf':Path(imported['path'])}
            if rounds:
                for label,name in EXTRAS.items():inputs['/input/'+name]=Path(imported[label]['path'])
            return {'settings':{'mode':'import_translation','sha256':imported['sha256'],'mapping':imported['mapping']},'inputs':inputs}
        return {'settings':{'mode':'translate'},'inputs':{'/input/background.json':previous_artifact(store,phase,'background'),
                '/input/background-pack.json':previous_artifact(store,phase,'backgroundPack')},
                'policy':{'model':'deepseek-flash','thinking':'disabled','maxOutputTokens':8192,'allowJsonRequest':True}}
    def finish(self,store,phase,record,result):
        folder=Path(record['work']);job=store.get(phase['job']);imported=job['config'].get('importTranslation')
        if imported:
            from repaired_import import validate_import,EXTRAS
            rounds=validate_import(imported,job['sourceSha256'],job['sourcePages'])
            if digest(imported['path'])!=imported['sha256'] or digest(folder/'mono.pdf')!=imported['sha256']:
                raise ValueError('Imported translation bytes changed')
            if read_bounded(folder/'mapping.json',folder,65536).get('sourcePages')!=imported['mapping']:
                raise ValueError('Imported translation page mapping changed')
            receipt=folder/'import-provenance.json'
            atomic(receipt,{'origin':imported,'status':'imported_unreviewed','oldAcceptanceInherited':False})
            artifacts={'mono':artifact(folder/'mono.pdf'),'mapping':artifact(folder/'mapping.json'),'importProvenance':artifact(receipt)}
            if rounds:
                for label,name in EXTRAS.items():
                    if digest(folder/name)!=imported[label]['sha256']:raise ValueError('Imported repair sidecar changed')
                    artifacts[label]=artifact(folder/name)
            return {'artifacts':artifacts}
        responses=delivered_responses(store,phase)
        if digest(folder/'mono.pdf')!=result.get('monoSha256') or digest(folder/'mapping.json')!=result.get('mappingSha256'):
            raise ValueError('Translation result artifact hash mismatch')
        mapping=read_bounded(folder/'mapping.json',folder,65536)
        if mapping.get('sourcePages')!=store.get(phase['job'])['sourcePages']:raise ValueError('Translation scope changed')
        receipt=folder/'provider-receipts.json'
        atomic(receipt,{'calls':[r['call'] for r in responses],'sourceSha256':record['sourceSha256'],
                        'status':'candidate_translation_not_accepted'})
        return {'artifacts':{'mono':artifact(folder/'mono.pdf'),'mapping':artifact(folder/'mapping.json'),'translationCalls':artifact(receipt)}}


class ComposeHandler:
    def prepare(self,store,phase,job):
        inputs={'/input/mono.pdf':previous_artifact(store,phase,'mono'),
                '/input/mapping.json':previous_artifact(store,phase,'mapping')}
        try:inputs['/input/supplements.json']=previous_artifact(store,phase,'supplements')
        except MissingArtifact:pass
        try:inputs['/input/source-quotes.json']=previous_artifact(store,phase,'sourceQuotes')
        except MissingArtifact:pass
        return {'settings':{'mode':'compose'},'inputs':inputs}
    def finish(self,store,phase,record,result):
        from candidate_ops import register_composition
        work=Path(record['work']);mono=work/'readable.pdf';mapping=work/'readable-mapping.json'
        layout=read_bounded(work/'composition/layout.json',work/'composition',4*1024*1024)
        proof=layout.get('readability',{})
        if (digest(mono)!=result.get('normalizedMonoSha256') or digest(mapping)!=result.get('normalizedMappingSha256') or
            proof.get('normalizedMonoSha256')!=digest(mono) or proof.get('normalizedMappingSha256')!=digest(mapping)):
            raise ValueError('Readable repair checkpoint differs from rendered composition')
        pages=read_bounded(mapping,work,65536)['sourcePages']
        if not pages or set(pages)!=set(store.get(phase['job'])['sourcePages']):raise ValueError('Readable repair page map changed')
        registered=register_composition(store,phase,record,result)
        # Repair the actual readable mono, not the old 7pt engine PDF. The file
        # excludes delivery headers, so later composition cannot stack headers
        # or reflow every small block into hundreds of fresh continuation pages.
        registered['artifacts'].update(mono=artifact(mono),mapping=artifact(mapping))
        return registered


class ReviewHandler:
    def prepare(self,store,phase,job):
        from review_protocol import begin_review
        plan,path=begin_review(store,phase)
        candidate=store.get(job['id'])['candidate']
        return {'settings':{'mode':'review'},'inputs':{'/input/review-plan.json':path,
                    '/input/candidate':Path(candidate['files']['cn']['path']).parent},
                'policy':{'model':'deepseek-flash','thinking':'enabled','maxOutputTokens':32768,'jsonOutput':True}}
    def finish(self,store,phase,record,result):
        from review_protocol import submit_review
        return submit_review(store,phase,record,result)


class RepairHandler:
    def prepare(self,store,phase,job):
        from repair_ops import POLICY
        candidate=store.verify(job,allow_failed_qa=True)
        row=store.db.execute('SELECT body FROM tickets WHERE id=?',(job.get('lastReview'),)).fetchone()
        if not row:raise ValueError('Independent review required before repair')
        ticket=json.loads(row[0]);report=ticket['report']
        if ticket['candidate']!=candidate['id'] or digest(report['path'])!=report['sha256']:raise ValueError('Repair review changed')
        review=read_json(report['path'])
        if review['verdict']!='needs_attention' or not 1<=phase['repairRound']<=2:raise ValueError('Invalid repair round')
        mono=previous_artifact(store,phase,'mono');mapping=previous_artifact(store,phase,'mapping')
        texts=read_json(candidate['files']['reviewText']['path'])
        plan={'candidateId':candidate['id'],'sourceSha256':job['sourceSha256'],'sourcePages':job['sourcePages'],
              'monoSha256':digest(mono),'repairRound':phase['repairRound'],'sourceTexts':texts['source'],
              'issues':review['issues'],'reviewSha256':report['sha256']}
        plan['sourceImages']={str(p):[{'name':name,'sha256':candidate['files'][name]['sha256']}
            for name in (f'preview/source-{p:04}.png',f'preview/source-{p:04}-top.png',f'preview/source-{p:04}-bottom.png')]
            for p in job['sourcePages']}
        if 'sourceQuotes' in candidate['files']:
            plan['sourceQuoteCandidates']=read_json(candidate['files']['sourceQuotes']['path'])['candidates']
        path=store.folder(job)/'repair-inputs'/f"generation-{phase['generation']}.json";atomic(path,plan)
        inputs={'/input/repair-input.json':path,'/input/mono.pdf':mono,'/input/mapping.json':mapping,
                '/input/candidate':Path(candidate['files']['cn']['path']).parent}
        try:inputs['/input/supplements.json']=previous_artifact(store,phase,'supplements')
        except MissingArtifact:pass
        return {'settings':{'mode':'repair'},'inputs':inputs,'policy':POLICY}

    def finish(self,store,phase,record,result):
        from repair_ops import POLICY,request_body,sanitize_response
        from review_protocol import checked_file
        from model_broker import provider_payload
        job=store.get(phase['job']);candidate=store.verify(job,allow_failed_qa=True);folder=Path(record['work'])
        path=folder/'repair-plan.json'
        if digest(path)!=result.get('planSha256'):raise ValueError('Repair plan changed')
        plan=read_bounded(path,folder,16*1024*1024)
        original=read_json(store.folder(job)/'repair-inputs'/f"generation-{phase['generation']}.json")
        if {k:v for k,v in plan.items() if k!='blocks'}!=original or result.get('candidateId')!=candidate['id']:
            raise ValueError('Repair plan input mismatch')
        if not isinstance(plan.get('blocks'),list) or len(plan['blocks'])>100000:raise ValueError('Invalid repair inventory')
        responses=delivered_responses(store,phase);path=folder/'repair-receipts.json'
        if digest(path)!=result.get('receiptSha256'):raise ValueError('Repair receipts changed')
        receipts=read_bounded(path,folder,4*1024*1024)['receipts']
        if [r.get('sourcePage') for r in receipts]!=plan['sourcePages']:raise ValueError('Repair page coverage mismatch')
        calls=[];verified_repairs=[]
        for receipt in receipts:
            matches=[r for r in responses if r['body'].get('id')==receipt['providerId'] and object_hash(r['body'])==receipt['responseHash']]
            if len(matches)!=1:raise ValueError('Repair response has no unique private receipt')
            matched=matches[0];call=json.loads(store.db.execute('SELECT body FROM model_calls WHERE id=?',(matched['call'],)).fetchone()[0])
            payload=read_json(checked_file(call['request'],store.root/'broker-private'))
            if call['policy']!=POLICY or payload!=provider_payload(request_body(plan,receipt['sourcePage'],Path(candidate['files']['cn']['path']).parent),POLICY):
                raise ValueError('Repair provider received different context')
            verified_repairs.append(sanitize_response(plan,receipt['sourcePage'],json.loads(matched['body']['choices'][0]['message']['content'])))
            calls.append(matched['call'])
        files=result.get('files',{})
        if set(files)!={'mono.pdf','mapping.json','supplements.json','repair-applied.json','source-quotes.json'}:raise ValueError('Incomplete repair output')
        for name,sha in files.items():
            if digest(folder/name)!=sha:raise ValueError('Repair output hash mismatch')
        from repair_ops import quote_candidates
        if read_json(folder/'source-quotes.json')!={'candidates':quote_candidates(plan,{r['sourcePage']:r for r in verified_repairs}),
            'status':'unverified_visual_transcription_candidates'}:
            raise ValueError('Visual source quotes differ from private provider responses')
        mapping=read_bounded(folder/'mapping.json',folder,65536)['sourcePages']
        if not mapping or any(type(p) is not int for p in mapping) or set(mapping)!=set(job['sourcePages']):
            raise ValueError('Repair lost source pages')
        seen=[]
        for page in mapping:
            if not seen or seen[-1]!=page:seen.append(page)
        if seen!=job['sourcePages']:raise ValueError('Repair source groups are out of order')
        memories=[];skipped=0
        try:
            registration=read_json(previous_artifact(store,phase,'corpusRegistration'))
            background=read_json(previous_artifact(store,phase,'background'))
        except MissingArtifact:registration=None
        if registration:
            import re
            corpus=CorpusIndex(store.db)
            with store.transaction():
                for response in verified_repairs:
                    for update in response['updates']:
                        quote=update['sourceQuote'];target=update['replacement'];matched=False
                        if len(quote)<=10000 and len(target)<=10000:
                            pattern=r'\s+'.join(re.escape(part) for part in quote.split())
                            for pid in registration['passages']:
                                passage=corpus.passage(pid)
                                match=re.search(pattern,passage['text']) if passage['page']==response['sourcePage'] else None
                                if match and len(match.group())<=10000:
                                    memories.append(corpus.add_translation('memory',match.group(),target,background['content']['domain'],pid,producer_job=job['id']))
                                    matched=True;break
                        if not matched:skipped+=1
        memory_path=folder/'repair-memory-candidates.json'
        atomic(memory_path,{'candidateIds':sorted(set(memories)),'skippedUnlocatableOrOversized':skipped,
                            'status':'candidate_only_requires_explicit_independent_review'})
        atomic(folder/'repair-provider-calls.json',{'calls':calls,'candidateId':candidate['id'],'status':'unaccepted_repair'})
        return {'artifacts':{'mono':artifact(folder/'mono.pdf'),'mapping':artifact(folder/'mapping.json'),
                'supplements':artifact(folder/'supplements.json'),'repairApplied':artifact(folder/'repair-applied.json'),
                'repairPlan':artifact(folder/'repair-plan.json'),'repairCalls':artifact(folder/'repair-provider-calls.json'),
                'sourceQuotes':artifact(folder/'source-quotes.json'),
                'repairMemoryCandidates':artifact(memory_path)}}
