"""Trusted library retrieval and offline single-PDF extraction/cache.

Only the controller owns Zotero access and index registration. A worker sees a
single frozen PDF, no full library, credentials, network or SQLite database.
"""
import json
from pathlib import Path
import re
import shutil
import time

from api_mailbox import read_bounded
from core import atomic,digest,object_hash,read_json
from corpus import CorpusIndex
from docker_runner import DockerRunner,linux_path
from zotero import key
from source_text import EXTRACTOR_VERSION


class PDFScanner:
    def __init__(self,root,runtime,image_lock):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.runner=DockerRunner(self.root,Path(runtime),image_lock,{})
        self.runtime=Path(runtime).resolve()

    def scan(self,source,*,timeout=120):
        source=Path(source).resolve(strict=True)
        if not source.is_file() or not 0<source.stat().st_size<=512*1024**2:
            raise ValueError('PDF input must be a nonempty file of at most 512 MiB')
        sha=digest(source);folder=self.root/EXTRACTOR_VERSION/sha;folder.mkdir(parents=True,exist_ok=True)
        receipt=folder/'receipt.json';output=folder/'output';output.mkdir(exist_ok=True)
        if receipt.exists():
            saved=read_json(receipt)
            if saved.get('extractorVersion')!=EXTRACTOR_VERSION or saved['sourceSha256']!=sha or saved['image']!=self.runner.image or digest(output/'scan.json')!=saved['outputSha256']:
                raise ValueError('Cached PDF extraction changed')
            return self.validate(output,sha),saved
        frozen=folder/'source.pdf'
        if shutil.disk_usage(self.root).free<2*1024**3+source.stat().st_size:
            raise OSError('Source storage is below its free-space threshold')
        if not frozen.exists():shutil.copyfile(source,frozen)
        if digest(frozen)!=sha:raise ValueError('Frozen corpus source changed')
        name='lit-scan-'+object_hash({'root':str(self.root),'source':sha,'image':self.runner.image,'extractorVersion':EXTRACTOR_VERSION})[:32];intent=folder/'intent.json'
        mounts=[(self.runtime,'/app/runtime',True),(frozen,'/input/source.pdf',True),(output,'/output',False)]
        mount_receipt=[{'source':linux_path(p),'target':t,'readonly':r} for p,t,r in mounts]
        expected={'worker':name,'image':self.runner.image,'sourceSha256':sha,'mounts':mount_receipt,'extractorVersion':EXTRACTOR_VERSION}
        if intent.exists():
            if read_json(intent)!=expected:raise ValueError('Existing scan intent changed')
        else:
            if not self.runner.storage_ready():raise OSError('Docker storage is below its free-space threshold')
            atomic(intent,expected)
            args=['run','--detach','--pull','never','--name',name,'--network','none','--read-only','--user','10001:10001',
                  '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128','--memory','1g','--cpus','1',
                  '--tmpfs','/tmp:rw,noexec,nosuid,size=64m']
            for m in mount_receipt:args.extend(['--mount',f'type=bind,source={m["source"]},target={m["target"]}'+(',readonly' if m['readonly'] else '')])
            args.extend([self.runner.image,'python','/app/runtime/pdf_scan_worker.py'])
            # A missing launch ACK never authorizes another launch.
            self.runner.docker(args,timeout=30)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            result=self.runner.docker(['container','inspect',name])
            if result.returncode:raise RuntimeError('Scan state unknown or missing; inspect existing intent before recovery')
            data=json.loads(result.stdout)[0];host=data['HostConfig']
            actual=sorted((m['Source'],m['Destination'],not m['RW']) for m in data['Mounts'] if m['Type']=='bind')
            expected_mounts=sorted((m['source'],m['target'],m['readonly']) for m in mount_receipt)
            if (data['Image']!=self.runner.image or data['Config']['User']!='10001:10001' or
                host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or 'ALL' not in (host.get('CapDrop') or []) or
                not any(s.startswith('no-new-privileges') for s in host.get('SecurityOpt',[])) or host.get('Privileged') or
                actual!=expected_mounts or host.get('Memory')!=1024**3 or host.get('NanoCpus')!=1_000_000_000 or
                host.get('PidsLimit')!=128 or host.get('CapAdd') or host.get('PidMode')=='host' or
                any(m['Type']!='bind' and not(m['Type']=='tmpfs' and m['Destination']=='/tmp') for m in data['Mounts'])):
                raise ValueError('PDF extraction isolation changed')
            if not data['State']['Running']:
                if data['State']['ExitCode']!=0:raise ValueError('PDF extraction failed; no retry or OCR fallback was inferred')
                value=self.validate(output,sha)
                saved={**expected,'outputSha256':digest(output/'scan.json'),'firstIndexedAt':time.time()}
                atomic(receipt,saved);return value,saved
            time.sleep(.2)
        # This is an observation timeout, not a claim that the worker stopped.
        raise TimeoutError('PDF scan still has an existing worker; resume inspection of the same identity')

    @staticmethod
    def validate(output,sha):
        data=read_bounded(output/'scan.json',output,32*1024*1024);info=data.get('sourceInfo',{})
        if data.get('sourceSha256')!=sha or type(info.get('pages')) is not int or not 1<=info['pages']<=2000:
            raise ValueError('PDF extraction identity/count mismatch')
        if [p.get('sourcePage') for p in data.get('pages',[])]!=list(range(1,info['pages']+1)):
            raise ValueError('Missing PDF extraction pages')
        if info.get('text_lengths')!=[p['originalTextLength'] for p in data['pages']]:raise ValueError('Extraction length receipt mismatch')
        if any(not isinstance(p['text'],str) or len(p['text'])>30000 or type(p['truncated']) is not bool for p in data['pages']):
            raise ValueError('Invalid extracted text schema')
        return data


def extract_attachment(client,scanner,parent,attachment,*,expected_sha=None):
    parent=key(parent);attachment=key(attachment)
    p=client.get('/users/0/items/'+parent);p=p.get('data',p)
    a=client.get('/users/0/items/'+attachment);a=a.get('data',a)
    if p.get('itemType') in (None,'attachment','note') or p.get('deleted') or a.get('parentItem')!=parent or a.get('contentType')!='application/pdf' or a.get('deleted'):
        raise ValueError('Related source is not a current PDF child of its registered parent')
    path=Path(client.file(attachment))
    if expected_sha is not None and digest(path)!=expected_sha:raise ValueError('Reconciled library source changed')
    scan,receipt=scanner.scan(path)
    current=client.get('/users/0/items/'+attachment);current=current.get('data',current)
    if current.get('deleted') or current.get('parentItem')!=parent or digest(Path(client.file(attachment)))!=scan['sourceSha256']:
        raise ValueError('Library source changed during extraction')
    reference={'kind':'zotero','parentItemKey':parent,'attachmentKey':attachment,'sha256':scan['sourceSha256'],
               'title':p.get('title','Untitled'),'retrievedAt':receipt['firstIndexedAt'],'extractionCoverage':scan['coverage'],
               'extractorVersion':scan.get('extractorVersion')}
    passages=[{'page':p['sourcePage'],'anchor':f'page-{p["sourcePage"]}-text','text':p['text']} for p in scan['pages'] if p['text'].strip()]
    return {'reference':reference,'passages':passages,'pages':scan['sourceInfo']['pages']}


def register_extraction(store,extracted):
    corpus=CorpusIndex(store.db);reference=extracted['reference'];passages=extracted['passages']
    result={'attachmentKey':reference['attachmentKey'],'parentItemKey':reference['parentItemKey'],
            'pages':extracted['pages'],'sourceSha256':reference['sha256']}
    if not passages:return {**result,'status':'no_extractable_text'}
    with store.transaction():registered=corpus.add_source(reference,passages)
    return {**result,'status':'indexed','source':registered['source'],'passages':len(registered['passages'])}


def import_attachment(store,client,scanner,parent,attachment,*,expected_sha=None):
    return register_extraction(store,extract_attachment(client,scanner,parent,attachment,expected_sha=expected_sha))
