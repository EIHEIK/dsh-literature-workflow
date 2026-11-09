"""Register isolated-renderer outputs without rendering PDFs on the controller."""
from pathlib import Path
import shutil
import uuid

from api_mailbox import read_bounded
from core import atomic,code_hash,digest,object_hash,read_json


def register_composition(store,phase,record,result):
    job=store.get(phase['job']);work=Path(record['work']).resolve();output=work/'composition'
    if output.is_symlink() or output.resolve().parent!=work:raise ValueError('Composition folder escaped its worker')
    layout=read_bounded(output/'layout.json',output,4*1024*1024)
    qa=read_bounded(output/'qa.json',output,4*1024*1024)
    if not isinstance(qa.get('hardFailures'),list) or qa.get('profile')!='strict-generated-cn-v1':
        raise ValueError('Strict program QA receipt required')
    for key in ('cnPages','biPages'):
        if type(layout.get(key)) is not int or not 1<=layout[key]<=2000:raise ValueError('Invalid composed page count')
    rows=layout.get('rows')
    if not isinstance(rows,list) or len(rows)!=layout['cnPages'] or layout['cnPages']!=layout['biPages']:
        raise ValueError('One original/Chinese pair is required for every delivered Chinese page')
    if ([r.get('cnPage') for r in rows]!=list(range(1,len(rows)+1)) or
        [r.get('biPage') for r in rows]!=list(range(1,len(rows)+1)) or
        set(r.get('sourcePage') for r in rows)!=set(job['sourcePages'])):raise ValueError('Incomplete page map')
    expected={'cn':'chinese.pdf','bi':'bilingual.pdf','qa':'qa.json','layout':'layout.json','reviewText':'review-text.json'}
    if 'sourceQuotes' in result.get('files',{}):expected['sourceQuotes']='source-quotes.json'
    for label,numbers in [('source',job['sourcePages']),('cn',range(1,len(rows)+1)),('bi',range(1,len(rows)+1))]:
        for number in numbers:
            name=f'preview/{label}-{number:04}.png';expected[name]=name
            if label!='bi':
                for half in ('top','bottom'):
                    name=f'preview/{label}-{number:04}-{half}.png';expected[name]=name
    announced=result.get('files')
    if not isinstance(announced,dict) or set(announced)!=set(expected):raise ValueError('Composition evidence inventory mismatch')
    origin={'job':phase['job'],'generation':phase['generation'],'files':announced}
    if (job.get('candidate') or {}).get('compositionOrigin')==origin:
        candidate=store.verify(job,allow_failed_qa=True)
        path=Path(candidate['files']['cn']['path']).parent/'candidate.json'
        if read_json(path)!=candidate:raise ValueError('Candidate registration receipt changed')
        return {'artifacts':{'candidate':{'path':str(path),'sha256':digest(path)}}}
    destination=store.folder(job)/'candidates'/uuid.uuid4().hex;destination.mkdir(parents=True)
    files={}
    for key,relative in expected.items():
        source=(output/relative).resolve(strict=True)
        if not source.is_relative_to(output.resolve()) or not source.is_file() or digest(source)!=announced[key]:
            raise ValueError('Changed or escaped composition artifact')
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        files[key]={'path':str(target),'sha256':digest(target)}
        if files[key]['sha256']!=announced[key]:raise ValueError('Candidate copy changed')
    manifest={'sourceSha256':job['sourceSha256'],'sourcePages':job['sourcePages'],'files':files,
              'codeHash':code_hash(),'configHash':object_hash(job['config']),'layout':layout,'compositionOrigin':origin}
    candidate={'id':'cand-'+object_hash(manifest)[:24],**manifest}
    atomic(destination/'candidate.json',candidate)
    with store.transaction():
        job.update(candidate=candidate,acceptance=None,publication=None,archive={},state='needs_review',lease=None,leaseUntil=0)
        job['producers']=sorted(set(job['producers']+['controller:producer']))
        store.save(job)
    return {'artifacts':{'candidate':{'path':str(destination/'candidate.json'),'sha256':digest(destination/'candidate.json')}}}
