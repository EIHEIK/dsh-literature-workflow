"""Populate a real local reference index from the already reconciled first batch.

Read-only Zotero, offline Docker extraction, no translation jobs or model calls.
"""
import argparse
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from batches import BatchStore
from core import atomic,read_json
from corpus import CorpusIndex
from instance_lock import InstanceLock
from library_index import PDFScanner,import_attachment
from zotero import Zotero


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--intake',required=True);parser.add_argument('--data',required=True)
    args=parser.parse_args();data=Path(args.data).resolve();data.mkdir(parents=True,exist_ok=True)
    intake=read_json(args.intake);results=[];client=Zotero()
    with InstanceLock(data):
        store=BatchStore(data)
        try:
            scanner=PDFScanner(data/'library-scan',ROOT/'runtime',ROOT/'deploy/engine-image.lock.json')
            for item in intake['ready']:
                try:
                    result=import_attachment(store,client,scanner,item['zotero_key'],item['attachmentKey'],expected_sha=item['sourceSha256'])
                    if result.get('sourceSha256')!=item['sourceSha256']:raise ValueError('Intake source changed')
                except (ValueError,OSError) as error:result={'id':item['id'],'status':'unavailable','errorType':type(error).__name__}
                results.append({'id':item['id'],**result})
                atomic(data/'library-index-progress.json',{'items':results,'total':len(intake['ready']),'modelCalls':0,'zoteroWrites':0})
                print(item['id']+': '+result['status'],flush=True)
            index=CorpusIndex(store.db);matches=index.search('aerodynamic flight simulation multi fidelity',limit=10)
            report={'at':time.time(),'items':results,'indexed':sum(r['status']=='indexed' for r in results),
                    'queryMatches':len(matches),'sourceKinds':sorted({m['reference']['kind'] for m in matches}),
                    'jobsCreated':store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],
                    'modelCalls':0,'zoteroWrites':0,'trustedTerms':store.db.execute('SELECT COUNT(*) FROM corpus_trust').fetchone()[0]}
            atomic(data/'library-index-report.json',report);print('Report: '+str(data/'library-index-report.json'))
        finally:store.db.close()


if __name__=='__main__':main()
