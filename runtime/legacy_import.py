"""Explicit maintenance migration of the existing three-page pilot, read-only legacy."""
import json
from pathlib import Path
import shutil
import sqlite3

from core import atomic,digest,object_hash,read_json,code_hash
from library_index import PDFScanner
from user_authorization import bind_existing_authorization
from zotero import Zotero


def prepare_pilot(store,legacy_root,intake,sessions,thread):
    legacy_root=Path(legacy_root).resolve(strict=True)
    db=sqlite3.connect((legacy_root/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        row=db.execute('SELECT body FROM jobs WHERE id=?',('paper-d9134d462a8945f7',)).fetchone()
        if row is None:raise ValueError('Existing pilot not found')
        old=json.loads(row[0])
    finally:db.close()
    if old['sourcePages']!=[11,12,13] or old['sourceInfo']['pages']!=20:raise ValueError('Unexpected legacy pilot scope')
    candidate=old['candidate'];translated=candidate['files']['cn'];mapping=[r['sourcePage'] for r in candidate['layout']['rows']]
    if digest(old['source'])!=old['sourceSha256'] or digest(translated['path'])!=translated['sha256']:
        raise ValueError('Legacy artifacts changed')
    if candidate['sourceSha256']!=old['sourceSha256'] or set(mapping)!=set(old['sourcePages']):raise ValueError('Legacy candidate identity differs')
    client=Zotero();zotero=old['zotero'];current=Path(client.file(zotero['sourceAttachmentKey']))
    meta=client.get('/users/0/items/'+zotero['sourceAttachmentKey']);meta=meta.get('data',meta)
    if meta.get('parentItem')!=zotero['parentItemKey'] or digest(current)!=old['sourceSha256']:
        raise ValueError('Current Zotero source differs')
    scan_folder=store.root/'library-scan'/old['sourceSha256'];scan=PDFScanner.validate(scan_folder/'output',old['sourceSha256'])
    scan_receipt=read_json(scan_folder/'receipt.json')
    if digest(scan_folder/'output/scan.json')!=scan_receipt['outputSha256']:raise ValueError('Source scan changed')
    origin={'legacyJob':old['id'],'legacyCandidate':candidate['id'],'sourceSha256':old['sourceSha256'],
            'translationSha256':translated['sha256'],'mapping':mapping,'scope':[11,12,13],
            'oldAcceptanceInherited':False,'historicalCosts':'retained in original legacy evidence; not reset or reported as zero'}
    folder=store.root/'imports'/object_hash(origin);folder.mkdir(parents=True,exist_ok=True);target=folder/'legacy-chinese.pdf'
    if not target.exists():shutil.copyfile(translated['path'],target)
    if digest(target)!=translated['sha256']:raise ValueError('Frozen legacy translation changed')
    atomic(folder/'migration.json',origin)
    config={'revision':'legacy-p11-13-independent-review-v2','runtimeRevision':code_hash(),'stageTimeoutSeconds':3600,'progressTimeoutSeconds':900,
            'importTranslation':{'path':str(target),'sha256':translated['sha256'],'mapping':mapping,'originHash':object_hash(origin)}}
    summary=store.prepare([{'source':str(current),'title':'FS002 部分译本 p11–13','pages':'11-13','zotero':zotero,'_sourceInfo':scan}],config,pilot=True,title='旧p11–13试点返修与重新验收')
    authorization=bind_existing_authorization(store,summary['batch'],intake,sessions,thread)
    result={'batch':summary['batch'],'summaryHash':summary['summaryHash'],'job':summary['items'][0]['job'],
            'migration':str(folder/'migration.json'),'authorization':authorization,'started':False,'modelCalls':0}
    atomic(store.root/'pilot-preparation.json',result)
    return result
