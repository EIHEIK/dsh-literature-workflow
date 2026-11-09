"""Durable local workflow. No model calls in this module; all acceptance is artifact-bound."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

CHECKS = ('faithfulness', 'complete_scope', 'formulas', 'figures', 'tables', 'page_mapping')

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def object_hash(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def identity(actor):
    if not isinstance(actor, str) or not actor.strip() or actor == 'unidentified':
        raise ValueError('A real Harness session identity is required for this operation')
    return actor

def pdf_info(path):
    import pymupdf as fitz
    with fitz.open(path) as d:
        if not d.is_pdf or d.needs_pass or not len(d): raise ValueError('Readable, nonempty, unencrypted PDF required')
        return {'pages': len(d), 'text_lengths': [len(p.get_text().strip()) for p in d]}

def page_scope(value, count):
    if value in (None, '', 'all'): return list(range(1, count + 1))
    result = set()
    for part in str(value).split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part.strip()): raise ValueError('pages must be 1,3-5 or all')
        pair = [int(x) for x in part.split('-')]; lo, hi = (pair * 2)[:2] if len(pair) == 1 else pair
        if not 1 <= lo <= hi <= count: raise ValueError('Page range outside source')
        result.update(range(lo, hi + 1))
    return sorted(result)

def code_hash():
    base = Path(__file__).parent
    return object_hash({p.name: digest(p) for p in sorted(base.glob('*.py'))})

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root/'state.sqlite3', timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.commit()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try: yield; self.db.commit()
        except BaseException: self.db.rollback(); raise

    def get(self, job):
        row = self.db.execute('SELECT body FROM jobs WHERE id=?', (job,)).fetchone()
        if not row: raise ValueError('Unknown job')
        return json.loads(row[0])

    def save(self, job):
        job['updatedAt'] = time.time()
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (job['id'], canonical(job)))

    def folder(self, job): return self.root/'jobs'/job['id']

    def create(self, a, actor):
        identity(actor)
        src = Path(a['source']).resolve(strict=True)
        info = pdf_info(src); sha = digest(src); scope = page_scope(a.get('pages'), info['pages'])
        config = a.get('config', {})
        for k in ('apiKey','api_key','token','authorization'):
            if k in config: raise ValueError('Credentials must not enter jobs')
        key = object_hash({'source': sha, 'scope': scope, 'zotero': a.get('zotero'), 'config': config})
        for body, in self.db.execute('SELECT body FROM jobs'):
            old = json.loads(body)
            if old['dedupKey'] == key: return old
        jid = 'paper-' + uuid.uuid4().hex[:16]; folder = self.root/'jobs'/jid; folder.mkdir(parents=True)
        target = folder/'source.pdf'; shutil.copyfile(src, target)
        if digest(target) != sha: raise ValueError('Source changed while copying')
        job = dict(id=jid, title=a.get('title') or src.stem, state='queued', generation=0,
                   createdAt=time.time(), source=str(target), sourceSha256=sha, sourceInfo=info,
                   sourcePages=scope, config=config, dedupKey=key, zotero=a.get('zotero'),
                   producers=[actor], candidate=None, acceptance=None, publication=None, archive={})
        self.save(job); return job

    def claim(self, jobid, actor):
        identity(actor); j = self.get(jobid)
        for body, in self.db.execute('SELECT body FROM jobs'):
            other=json.loads(body)
            if other['id']!=jobid and other.get('leaseUntil',0)>time.time():
                raise ValueError('RC1 allows one active paper worker globally; queue the next paper')
        if j['state'] in ('accepted', 'published', 'archived'): raise ValueError('Accepted job cannot be retranslated; create a revised job/config')
        if j.get('leaseUntil', 0) > time.time(): raise ValueError('Job has a live lease; do not duplicate it')
        j['generation'] += 1; j['lease'] = uuid.uuid4().hex; j['leaseUntil'] = time.time()+300
        j['state'] = 'running'; j['candidate'] = None; j['acceptance'] = None
        j['producers'] = sorted(set(j['producers'] + [actor])); j['error'] = None
        j['attempt'] = str(self.folder(j)/'attempts'/f"{j['generation']}-{uuid.uuid4().hex[:8]}")
        Path(j['attempt']).mkdir(parents=True); self.save(j); return j

    def lease(self, j, token):
        if j.get('lease') != token or j['state'] != 'running' or j.get('leaseUntil', 0) < time.time():
            raise ValueError('Expired/cancelled/stale worker lease')

    def heartbeat(self, jobid, token):
        j = self.get(jobid); self.lease(j, token); j['leaseUntil'] = time.time()+300; self.save(j)

    def cancel(self, jobid):
        j = self.get(jobid)
        if j['state'] in ('accepted','published','archived'): raise ValueError('Cannot cancel an accepted publication')
        j['generation'] += 1; j['lease'] = None; j['leaseUntil'] = 0; j['state'] = 'cancelled';j['acceptance']=None;j.pop('lastReview',None);self.save(j); return j

    def failure(self, jobid, token, message):
        j = self.get(jobid)
        if j.get('lease') != token: return j
        j.update(state='needs_attention', leaseUntil=0, lease=None, error=str(message)[:2000]); self.save(j); return j

    def stage(self, jobid, a, actor, token=None):
        from pdf_ops import compose
        identity(actor); j = self.get(jobid)
        if token: self.lease(j, token)
        elif j.get('leaseUntil',0) > time.time(): raise ValueError('Cannot revise while a worker is running')
        if j['state'] in ('published','archived'): raise ValueError('Keep published job immutable; use a new job/config revision')
        if digest(j['source']) != j['sourceSha256']: raise ValueError('Source has changed')
        mono = Path(a['mono']).resolve(strict=True); info = pdf_info(mono)
        mapping = a.get('mapping')
        if mapping is None:
            if info['pages'] != len(j['sourcePages']): raise ValueError('Explicit source-page mapping required for reflow/continuations')
            mapping = j['sourcePages']
        if len(mapping) != info['pages'] or any(type(x) is not int or x not in j['sourcePages'] for x in mapping):
            raise ValueError('Invalid per-translated-page source mapping')
        if set(mapping) != set(j['sourcePages']): raise ValueError('Missing source pages (including deleted tail pages)')
        folder = self.folder(j)/'candidates'/uuid.uuid4().hex; folder.mkdir(parents=True)
        copied = folder/'engine-mono.pdf'; shutil.copyfile(mono, copied)
        supplements = a.get('supplements', [])
        for item in supplements:
            if item.get('sourcePage') not in j['sourcePages'] or not isinstance(item.get('text'), str):
                raise ValueError('Supplement requires sourcePage and text')
        layout = compose(j['source'], str(copied), mapping, supplements, folder)
        files = {k: {'path': str(folder/f), 'sha256': digest(folder/f)} for k,f in [('cn','chinese.pdf'),('bi','bilingual.pdf'),('qa','qa.json'),('layout','layout.json')]}
        for p in sorted((folder/'preview').glob('*.png')):
            files['preview/'+p.name] = {'path': str(p), 'sha256': digest(p)}
        manifest = dict(sourceSha256=j['sourceSha256'], sourcePages=j['sourcePages'], files=files,
                        codeHash=code_hash(), configHash=object_hash(j['config']), layout=layout)
        cid = 'cand-' + object_hash(manifest)[:24]
        candidate = dict(id=cid, **manifest)
        atomic(folder/'candidate.json', candidate)
        j.update(candidate=candidate, acceptance=None, publication=None, archive={}, state='needs_review', lease=None, leaseUntil=0)
        j['producers'] = sorted(set(j['producers']+[actor])); self.save(j); return j

    def verify(self, j):
        c = j.get('candidate')
        if not c: raise ValueError('No candidate')
        if digest(j['source']) != c['sourceSha256'] or c['sourceSha256'] != j['sourceSha256']: raise ValueError('Source identity mismatch')
        if c['codeHash'] != code_hash(): raise ValueError('Workflow code changed; restage and review before accepting')
        if c['configHash'] != object_hash(j['config']): raise ValueError('Configuration changed')
        for f in c['files'].values():
            if digest(f['path']) != f['sha256']: raise ValueError('Candidate/evidence file changed: '+Path(f['path']).name)
        if read_json(c['files']['qa']['path'])['hardFailures']: raise ValueError('Deterministic QA failed')
        return c

    def review_begin(self, jobid, actor):
        identity(actor); j = self.get(jobid); c = self.verify(j)
        if actor in j['producers']: raise ValueError('Review requires another real Harness session')
        tid = 'review-' + uuid.uuid4().hex
        report = dict(ticket=tid, candidateId=c['id'], sourceSha256=c['sourceSha256'],
                      hashes={k:c['files'][k]['sha256'] for k in ('cn','bi')},
                      semanticSourcePages=[], visualCnPages=[], visualBiPages=[],
                      checks={k:False for k in CHECKS}, issues=[], verdict='needs_attention', summary='')
        template = self.folder(j)/'reviews'/f'{tid}.template.json'; atomic(template, report)
        ticket = dict(id=tid, job=jobid, candidate=c['id'], actor=actor, createdAt=time.time(), report=None)
        self.db.execute('INSERT INTO tickets VALUES (?,?)',(tid,canonical(ticket)))
        return dict(ticket=tid, template=str(template), source=j['source'], candidate=c,
                    instructions='Read actual source and both PDFs; all pages and every required check must be covered. Do not echo this template as approval.')

    def review_submit(self, jobid, a, actor):
        identity(actor); j=self.get(jobid); c=self.verify(j); r=read_json(a['report'])
        row=self.db.execute('SELECT body FROM tickets WHERE id=?',(r.get('ticket'),)).fetchone()
        if not row: raise ValueError('Unregistered review ticket')
        t=json.loads(row[0])
        if t['job']!=jobid or t['candidate']!=c['id'] or t['actor']!=actor or actor in j['producers']:
            raise ValueError('Review identity/candidate mismatch')
        if t['report']: raise ValueError('Review already submitted; open a fresh ticket')
        if r.get('candidateId')!=c['id'] or r.get('sourceSha256')!=j['sourceSha256']: raise ValueError('Wrong review candidate/source')
        if r.get('hashes')!={k:c['files'][k]['sha256'] for k in ('cn','bi')}: raise ValueError('Review artifact hash mismatch')
        for field, expected in [('semanticSourcePages',j['sourcePages']),('visualCnPages',list(range(1,c['layout']['cnPages']+1))),('visualBiPages',list(range(1,c['layout']['biPages']+1)))]:
            if not isinstance(r.get(field),list) or any(type(x) is not int for x in r[field]) or sorted(set(r[field])) != sorted(expected):
                raise ValueError('Incomplete or invalid review coverage: '+field)
        if set(r.get('checks',{})) != set(CHECKS) or any(type(x) is not bool for x in r['checks'].values()): raise ValueError('Invalid check schema')
        if r.get('verdict') not in ('pass','needs_attention') or not isinstance(r.get('summary'),str) or not r['summary'].strip(): raise ValueError('Review verdict and evidence summary required')
        if not isinstance(r.get('issues'),list): raise ValueError('issues must be an array')
        for i in r['issues']:
            if not isinstance(i,dict) or not i.get('id') or not i.get('detail') or i.get('severity') not in ('blocker','major','minor','info') or i.get('status') not in ('open','closed'):
                raise ValueError('Invalid issue: require id/detail/severity/status')
        if r['verdict']=='pass' and (not all(r['checks'].values()) or any(i['status']=='open' and i['severity'] in ('blocker','major') for i in r['issues'])):
            raise ValueError('Cannot pass unresolved mandatory checks')
        path=self.folder(j)/'reviews'/f"{t['id']}.json";atomic(path,r)
        t['report']={'path':str(path),'sha256':digest(path)}
        self.db.execute('UPDATE tickets SET body=? WHERE id=?',(canonical(t),t['id']))
        j['lastReview']=t['id'];j['state']='reviewed' if r['verdict']=='pass' else 'needs_attention'; self.save(j);return j

    def acceptance(self,j):
        c=self.verify(j)
        row=self.db.execute('SELECT body FROM tickets WHERE id=?',(j.get('lastReview'),)).fetchone()
        if not row: raise ValueError('No registered independent review')
        t=json.loads(row[0]);f=t.get('report')
        if t['candidate']!=c['id'] or t['actor'] in j['producers'] or not f or digest(f['path'])!=f['sha256']: raise ValueError('Review dependency invalid')
        r=read_json(f['path'])
        if r['verdict']!='pass': raise ValueError('Review has not passed')
        return dict(candidate=c['id'], review=t['id'], reviewFile=f, files=c['files'], codeHash=c['codeHash'])

    def accept(self,jobid):
        j=self.get(jobid);j['acceptance']=self.acceptance(j);j['state']='accepted';self.save(j);return j

    def publish(self,jobid):
        j=self.get(jobid)
        if not j.get('acceptance') or j['acceptance']!=self.acceptance(j): raise ValueError('Valid acceptance required')
        c=j['candidate']; folder=self.root/'output'/jobid/c['id'];folder.mkdir(parents=True,exist_ok=True)
        outputs={}
        for mode in ('cn','bi'):
            target=folder/('chinese.pdf' if mode=='cn' else 'bilingual.pdf'); src=c['files'][mode]
            if target.exists():
                if digest(target)!=src['sha256']: raise ValueError('Existing publication corrupted; refusing to bless or overwrite it')
            else:
                temp=folder/(target.name+'.'+uuid.uuid4().hex+'.tmp');shutil.copyfile(src['path'],temp)
                if digest(temp)!=src['sha256']: raise ValueError('Publication copy changed')
                os.replace(temp,target)
            pdf_info(target);outputs[mode]={'path':str(target),'sha256':digest(target)}
        publication={'id':'pub-'+c['id'],'candidate':c['id'],'outputs':outputs}
        path=folder/'publication.json'
        if path.exists() and read_json(path)!=publication: raise ValueError('Publication manifest conflict')
        atomic(path,publication)
        j['publication']=publication;j['state']='published';self.save(j);return j

    def resume(self,jobid):
        j=self.get(jobid)
        if j['state']=='running':
            if j.get('leaseUntil',0)>time.time(): return {**j,'resumeHint':'Worker lease still live; query status, do not launch another worker'}
            j.update(state='interrupted',lease=None,leaseUntil=0);self.save(j)
        if j.get('candidate'): self.verify(j)
        if j.get('acceptance') and j['acceptance']!=self.acceptance(j): raise ValueError('Acceptance expired')
        if j.get('publication'):
            for f in j['publication']['outputs'].values():
                if digest(f['path'])!=f['sha256']: raise ValueError('Published artifact corrupt/missing')
        return j

def dispatch(store,a,actor):
    action=a['action']; jid=a.get('job')
    if action in ('zotero_inventory','create_zotero'):
        from zotero import Zotero,key
        client=Zotero(a.get('zoteroApi','http://127.0.0.1:23119/api'))
        if action=='zotero_inventory':
            if a.get('parentItemKey'):return client.children(a['parentItemKey'])
            start=int(a.get('start',0))
            if start<0:raise ValueError('start must be nonnegative')
            return client.get('/users/0/collections/'+key(a['collectionKey'])+'/items/top?limit=100&start='+str(start))
        attachment=key(a['sourceAttachmentKey']);meta=client.get('/users/0/items/'+attachment);data=meta.get('data',meta)
        if data.get('parentItem')!=key(a['parentItemKey']) or data.get('contentType')!='application/pdf':raise ValueError('Source attachment does not belong to parent or is not PDF')
        return store.create({**a,'source':client.file(attachment),'zotero':{'libraryType':'user','parentItemKey':a['parentItemKey'],'sourceAttachmentKey':attachment}},actor)
    if action=='list': return [json.loads(x[0]) for x in store.db.execute('SELECT body FROM jobs')]
    if action=='status': return store.get(jid)
    if action=='create': return store.create(a,actor)
    if action=='revise':
        old=store.get(jid)
        if not isinstance(a.get('revision'),str) or not a['revision'].strip():raise ValueError('Explicit revision label required for an idempotent fork')
        return store.create({'source':old['source'],'pages':','.join(map(str,old['sourcePages'])),'title':old['title'],
                             'config':{**old['config'],'parentJob':old['id'],'revision':a['revision']},'zotero':old.get('zotero')},actor)
    if action=='claim': return store.claim(jid,actor)
    if action=='cancel': return store.cancel(jid)
    if action=='stage': return store.stage(jid,a,actor)
    if action=='review_begin': return store.review_begin(jid,actor)
    if action=='review_submit': return store.review_submit(jid,a,actor)
    if action=='accept': return store.accept(jid)
    if action=='publish': return store.publish(jid)
    if action=='resume': return store.resume(jid)
    if action=='archive':
        from zotero import archive
        return archive(store,jid,a)
    raise ValueError('Unknown operation')

if __name__=='__main__':
    try:
        req=json.load(sys.stdin); s=Store(req['root'])
        with s.transaction(): result=dispatch(s,req['args'],req.get('actor','unidentified'))
        print(json.dumps({'ok':True,'result':result},ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'ok':False,'error':str(e)},ensure_ascii=False));sys.exit(1)
