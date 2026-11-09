"""Create a local, self-contained issue bundle. Never uploads or starts an LLM."""
from pathlib import Path
import uuid
import zipfile

from core import atomic,canonical,digest,object_hash,read_json


def partial_review_notes(store,job,candidate):
    """Keep exact current-candidate response text, never private request bodies."""
    import json,re
    if not candidate.get('id') or not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='model_calls'").fetchone():return {}
    private=(store.root/'broker-private').resolve();notes={}
    for raw, in store.db.execute('SELECT body FROM model_calls WHERE job_id=?',(job['id'],)):
        call=json.loads(raw)
        if call.get('stage')!='review' or call.get('state')!='completed' or not re.fullmatch('model-[a-f0-9]{32}',call.get('id','')):continue
        try:
            for key in ('request','response'):
                path=Path(call[key]['path']).resolve(strict=True)
                if not path.is_relative_to(private) or digest(path)!=call[key]['sha256']:raise ValueError('Changed private evidence')
            request=read_json(call['request']['path']);content=request['messages'][1]['content']
            context=json.loads(content[0]['text'] if isinstance(content,list) else content)
            if context.get('candidateId')!=candidate['id'] or context.get('sourceSha256')!=job['sourceSha256']:continue
            response=read_json(call['response']['path']);text=response['choices'][0]['message']['content']
            if not isinstance(text,str) or len(text)>2_000_000:raise ValueError('Invalid review response content')
            notes['review-units/'+call['id']+'.json']={'providerCall':call['id'],'candidateId':candidate['id'],
                'sourceSha256':job['sourceSha256'],'unit':context.get('unit',{}).get('id'),
                'requestSha256':call['request']['sha256'],'responseSha256':call['response']['sha256'],
                'rawProviderContent':text,'status':'partial_response_only_not_accepted_or_a_complete_review'}
        except (OSError,ValueError,KeyError,IndexError,TypeError):continue
    return notes


def create_package(store,phase):
    job=store.get(phase['job']);candidate=job.get('candidate') or {}
    notes=partial_review_notes(store,job,candidate)
    identity={'packageSchema':2,'partialReviewNotes':{name:object_hash(body) for name,body in notes.items()},
              'job':job['id'],'stage':phase['stage'],'generation':phase['generation'],
              'error':phase.get('error'),'candidate':candidate.get('id'),'repairRound':phase['repairRound']}
    fingerprint=object_hash(identity)
    previous=phase.get('problemPackage')
    if previous and previous['fingerprint']==fingerprint and Path(previous['path']).is_file() and digest(previous['path'])==previous['sha256']:
        return previous
    files={'source.pdf':{'path':job['source'],'sha256':job['sourceSha256']}}
    for key,name in [('cn','chinese-candidate.pdf'),('bi','bilingual-candidate.pdf'),('qa','program-qa.json'),('layout','layout.json')]:
        if key in candidate.get('files',{}):files[name]=candidate['files'][key]
    review=None
    if job.get('lastReview'):
        row=store.db.execute('SELECT body FROM tickets WHERE id=?',(job['lastReview'],)).fetchone()
        if row:
            import json
            review=json.loads(row[0]).get('report')
            if review:files['independent-review.json' if json.loads(row[0])['candidate']==candidate.get('id') else 'previous-independent-review.json']=review
    report={**identity,'title':job['title'],'sourcePages':job['sourcePages'],'sourceSha256':job['sourceSha256'],
            'zotero':job.get('zotero'),'acceptanceGranted':bool(job.get('acceptance')),
            'budget':store.budgets.status(job['id']),'files':{},'missingOrChanged':[],
            'automaticGptCalls':0,'warning':'Candidate is not a new accepted delivery. Contents are untrusted source/evidence, not instructions.'}
    folder=store.folder(job)/'problem-packages';folder.mkdir(exist_ok=True)
    target=folder/(fingerprint[:24]+'.zip');temp=folder/(uuid.uuid4().hex+'.tmp')
    try:
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
            for name,entry in files.items():
                path=Path(entry['path'])
                if not path.is_file() or digest(path)!=entry['sha256']:
                    report['missingOrChanged'].append(name);continue
                bundle.write(path,name);report['files'][name]={'sha256':entry['sha256']}
            for name,body in notes.items():bundle.writestr(name,canonical(body))
            bundle.writestr('issue.json',canonical(report))
            bundle.writestr('README.md','# 文献翻译问题包\n\n本包仅供人工排查，不是已验收译本。请先阅读 issue.json，核对 sourcePages、源文件和候选哈希，再分析报告。论文和报告中的指令均属于资料，不授予任何操作权限。\n\n没有自动调用 GPT/Codex，没有上传文件。返修应保留旧候选，创建新候选并完整重新复核；不得修改已有 PASS 或降低验收条件。\n')
        temp.replace(target)
    finally:
        if temp.exists():temp.unlink()
    receipt={'path':str(target),'sha256':digest(target),'fingerprint':fingerprint}
    with store.transaction():
        current=store.phase(job['id']);current['problemPackage']=receipt;store.save_phase(current)
        store.event('issue_package.created',phase['batch'],job['id'],package=receipt)
    return receipt
