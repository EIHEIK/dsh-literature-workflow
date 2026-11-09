"""Read-only reconciliation of the user's explicitly referenced priority list.

No new Zotero records, attachments, model requests or translation jobs are made.
Output belongs to the private workflow folder, never a public release artifact.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,digest,pdf_info
from zotero import Zotero,key

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--records',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();source=Path(args.records).resolve(strict=True)
    records=json.loads(source.read_text(encoding='utf-8'));client=Zotero()
    ready=[];pending=[];excluded=[];seen=set()
    for record in sorted(records,key=lambda r:r['rank']):
        base={k:record.get(k) for k in ('id','title','rank','batch','zotero_key','pdf_path','translation_status')}
        if record['language']=='中文' or record['translation_status']!='待翻译':
            # A missing-fulltext translation row still belongs in the pending list.
            if record['language']=='中文' or record.get('translation_files'):
                excluded.append({**base,'reason':'Chinese original or existing translation in the reviewed list'});continue
        parent=client.get('/users/0/items/'+key(record['zotero_key']))
        data=parent.get('data',parent)
        if data.get('deleted') or data.get('itemType') in ('attachment','note'):
            pending.append({**base,'reason':'Parent record unavailable'});continue
        children=client.children(record['zotero_key']);attachments=[];translations=[]
        for child in children:
            d=child.get('data',child)
            if d.get('itemType')!='attachment' or d.get('deleted'):continue
            k=child.get('key',d.get('key'));title=d.get('title','')
            if re.search(r'中文|译文|译本|双语|中英|translation|bilingual',title,re.I):translations.append({'key':k,'title':title})
            if d.get('contentType')!='application/pdf':continue
            try:
                path=Path(client.file(k));attachments.append({'key':k,'title':title,'path':str(path),'sha256':digest(path),**pdf_info(path)})
            except (ValueError,OSError):attachments.append({'key':k,'title':title,'unavailable':True})
        path=Path(record['pdf_path']) if record.get('pdf_path') else None
        if path is None or not path.is_file():
            pending.append({**base,'reason':'Listed source PDF unavailable','attachments':attachments});continue
        fingerprint=digest(path);matches=[a for a in attachments if a.get('sha256')==fingerprint]
        if not matches or translations:
            pending.append({**base,'reason':'Existing translation requires reconciliation' if translations else 'Listed source is not matched to a current Zotero attachment',
                            'attachments':attachments,'existingTranslations':translations});continue
        if fingerprint in seen:
            excluded.append({**base,'reason':'Duplicate source SHA256'});continue
        seen.add(fingerprint);attachment=matches[0]
        ready.append({**base,'sourceSha256':fingerprint,'pages':attachment['pages'],
                      'attachmentKey':attachment['key'],'currentAttachment':attachment['path'],
                      'currentTitle':data.get('title'),'sourceScope':'all'})
    report={'inspectedAt':time.time(),'sourceThread':'01a1b41a-f7c7-7892-9ce4-275a56e36501',
            'recordsPath':str(source),'recordsSha256':digest(source),'ready':ready,'pending':pending,'excluded':excluded,
            'authorization':'User requested this reviewed pending list as first batch after successful workflow acceptance.',
            'started':False,'modelCalls':0,'zoteroWrites':0}
    atomic(args.output,report)
    print(json.dumps({'ready':len(ready),'pages':sum(r['pages'] for r in ready),'pending':len(pending),'excluded':len(excluded),
                      'ids':[r['id'] for r in ready],'pendingReasons':[{k:r[k] for k in ('id','reason')} for r in pending],
                      'output':str(Path(args.output).resolve())},ensure_ascii=False))

if __name__=='__main__':main()
