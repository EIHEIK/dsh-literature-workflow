"""Narrow local MCP import bridge for Zotero 10, separate from frozen workers.

Only immutable, currently accepted workflow publications may be imported. The
native local API credential stays in this process and is never sent to MCP.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Redirect refused')


class Native:
    BASE='http://127.0.0.1:23119/api'
    def __init__(self,config):
        self.config=config;self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request(self,path,method='GET',body=None,headers=None):
        h={'Zotero-API-Version':'3','Zotero-Server-ID':self.config['serverID']}
        if method!='GET':h['Zotero-API-Key']=self.config['key']
        h.update(headers or {})
        if isinstance(body,(dict,list)):body=json.dumps(body).encode();h['Content-Type']='application/json'
        request=urllib.request.Request(self.BASE+path,data=body,headers=h,method=method)
        with self.opener.open(request,timeout=60) as response:raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('Native response too large')
        if not raw:return None
        try:return json.loads(raw)
        except ValueError:return raw.decode('utf-8')
    def children(self,parent):
        result=[]
        for start in range(0,10000,100):
            rows=self.request(f'/users/0/items/{parent}/children?limit=100&start={start}');result.extend(rows)
            if len(rows)<100:return result
        raise ValueError('Child inventory limit exceeded')
    def matches(self,item,sha):
        try:
            url=self.request(f'/users/0/items/{item}/file/view/url');parsed=urllib.parse.urlparse(url)
            if parsed.scheme!='file' or parsed.netloc not in ('','localhost'):raise ValueError('Non-local file URL')
            path=Path(urllib.request.url2pathname(parsed.path));actual=hashlib.sha256(path.read_bytes()).hexdigest()
        except urllib.error.HTTPError as error:
            if error.code==404:return False
            raise
        except FileNotFoundError:return False
        if actual!=sha:raise ValueError('Existing attachment has different bytes')
        return True
    def upload(self,item,path):
        raw=path.read_bytes();md5=hashlib.md5(raw).hexdigest();endpoint=f'/users/0/items/{item}/file'
        headers={'Content-Type':'application/x-www-form-urlencoded','If-None-Match':'*'}
        form=urllib.parse.urlencode({'md5':md5,'filename':path.name,'filesize':len(raw),'mtime':int(path.stat().st_mtime*1000)}).encode()
        result=self.request(endpoint,'POST',form,headers)
        if not result.get('exists'):
            url=urllib.parse.urlparse(result['url'])
            if (url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.port!=23119 or url.query or url.fragment or
                url.username or not re.fullmatch('/api/local/uploads/[A-Za-z0-9_-]+',url.path) or result.get('prefix') or result.get('suffix')):
                raise ValueError('Unexpected native upload destination')
            request=urllib.request.Request('http://127.0.0.1:23119'+url.path,raw,
                {'Content-Type':result['contentType'],'Zotero-Server-ID':self.config['serverID']},method='POST')
            with self.opener.open(request,timeout=90) as response:
                if response.status!=201:raise ValueError('Native upload did not complete')
            self.request(endpoint,'POST',urllib.parse.urlencode({'upload':result['uploadKey']}).encode(),headers)


class Gateway:
    def __init__(self,path):
        self.path=Path(path).resolve();self.config=json.loads(self.path.read_text(encoding='utf-8'))
        service=json.loads(Path(self.config['serviceConfig']).read_text(encoding='utf-8'))
        self.data=Path(service['data']).resolve();sys.path.insert(0,service['runtime'])
        from core import atomic,digest,object_hash,Store
        self.atomic=atomic;self.digest=digest;self.hash=object_hash;self.Store=Store
        self.native=Native(self.config);self.lock=threading.Lock();self.intents=self.path.parent/'zotero-import-intents';self.intents.mkdir(exist_ok=True)
    def authorize(self,parent,path,title):
        store=self.Store.__new__(self.Store);store.root=self.data
        store.db=sqlite3.connect((self.data/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:
            for raw, in store.db.execute('SELECT body FROM jobs'):
                job=json.loads(raw[0:] if isinstance(raw,bytes) else raw)
                if (job.get('zotero') or {}).get('parentItemKey')!=parent or not job.get('publication'):continue
                for mode,artifact in job['publication']['outputs'].items():
                    if str(Path(artifact['path']))!=path:continue
                    from zotero import scope_title
                    expected=('中文阅读版' if mode=='cn' else '中英左右对照')+scope_title(job)+' · '+job['candidate']['id'][-12:]
                    if title!=expected or store.acceptance(job)!=job['acceptance']:raise ValueError('Acceptance/title changed')
                    source=Path(path)
                    if source.is_symlink() or not source.resolve().is_relative_to(self.data/'jobs') or self.digest(source)!=artifact['sha256']:
                        raise ValueError('Publication bytes/path changed')
                    return {'job':job['id'],'sha256':artifact['sha256'],'path':path,'parent':parent,'title':title}
        finally:store.db.close()
        raise PermissionError('Only currently accepted registered publications may be imported')
    def import_file(self,args):
        if set(args)!={'action','filePath','parentItemKey','title','linkMode'} or args['action']!='import' or args['linkMode']!='imported_file':
            raise ValueError('Only fixed imported PDF operation is available')
        with self.lock:
            allowed=self.authorize(args['parentItemKey'],args['filePath'],args['title']);path=Path(allowed['path'])
            if path.stat().st_size>128*1024*1024:raise ValueError('Publication exceeds native upload bound')
            identity=self.hash(allowed);receipt=self.intents/(identity+'.json')
            record=json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {**allowed,'state':'pending'}
            self.atomic(receipt,record)
            staging='[翻译导入中] '+identity[:24];found=[]
            for item in self.native.children(allowed['parent']):
                data=item.get('data',item)
                if data.get('parentItem')==allowed['parent'] and data.get('title') in (allowed['title'],staging):found.append((item.get('key',data.get('key')),data))
            if len(found)>1:raise ValueError('Ambiguous attachment import')
            if found:key,data=found[0]
            else:
                template=self.native.request('/items/new?itemType=attachment&linkMode=imported_file')
                template.update(parentItem=allowed['parent'],title=staging,filename=path.name,contentType='application/pdf')
                created=self.native.request('/users/0/items','POST',[template]);key=created['successful']['0']['key'];data=created['successful']['0']['data']
            record['attachmentKey']=key;self.atomic(receipt,record)
            if not self.native.matches(key,allowed['sha256']):self.native.upload(key,path)
            if not self.native.matches(key,allowed['sha256']):raise ValueError('Uploaded PDF not verified')
            item=self.native.request('/users/0/items/'+key);data=item.get('data',item)
            if data.get('title')!=allowed['title']:
                self.native.request('/users/0/items/'+key,'PATCH',{'title':allowed['title']},{'If-Unmodified-Since-Version':str(item['version'])})
            record.update(state='verified');self.atomic(receipt,record)
            return {'attachmentKey':key,'sha256':allowed['sha256'],'verified':True}

    def recover_pending_once(self,now=None):
        """Bound retries; revoked keys/changed libraries never cause dialogs."""
        now=time.time() if now is None else now
        for receipt in self.intents.glob('*.json'):
            try:record=json.loads(receipt.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue # Never overwrite corrupt evidence.
            if record.get('state')!='pending' or record.get('retryAfter',0)>now:continue
            attempt=record.get('recoveryAttempts',0)+1
            if attempt>6:
                record.update(state='needs_attention',errorClass='RecoveryLimit');self.atomic(receipt,record);continue
            record.update(recoveryAttempts=attempt,lastRecoveryAt=now)
            self.atomic(receipt,record)
            try:
                self.import_file({'action':'import','filePath':record['path'],'parentItemKey':record['parent'],
                                  'title':record['title'],'linkMode':'imported_file'})
                continue
            except urllib.error.HTTPError as error:
                state=('authorization_required' if error.code in (401,403) else
                       'server_identity_changed' if error.code==412 else
                       'pending' if error.code>=500 or error.code==429 else 'needs_attention')
                error_class='HTTP'+str(error.code)
            except (urllib.error.URLError,TimeoutError,ConnectionError):
                state='pending';error_class='TransportUnavailable'
            except Exception as error:
                state='needs_attention';error_class=type(error).__name__
            # import_file may have committed an attachment key before losing
            # its ACK. Keep that newer evidence rather than restoring old data.
            latest=json.loads(receipt.read_text(encoding='utf-8'))
            if latest.get('state')=='verified':continue
            latest.update(state=state,errorClass=error_class,recoveryAttempts=attempt,
                          retryAfter=now+min(900,30*2**(attempt-1)))
            self.atomic(receipt,latest)


def serve(path):
    gateway=Gateway(path)
    def recover():
        while True:
            gateway.recover_pending_once()
            threading.Event().wait(30)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            try:
                if self.path!='/mcp' or self.headers.get('Origin') or self.headers.get('Host')!='127.0.0.1:23120':raise PermissionError()
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536:raise ValueError()
                message=json.loads(self.rfile.read(size));method=message.get('method');params=message.get('params',{})
                if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'accepted-publication-import','version':'1'}}
                elif method=='notifications/initialized':self.send_response(204);self.end_headers();return
                elif method=='tools/list':result={'tools':[{'name':'write_item','inputSchema':{'type':'object','properties':{k:{'type':'string'} for k in ('action','filePath','parentItemKey','title','linkMode')}}}]}
                elif method=='tools/call' and params.get('name')=='write_item':result={'content':[{'type':'text','text':json.dumps(gateway.import_file(params['arguments']))}]}
                else:raise ValueError()
                response={'jsonrpc':'2.0','id':message.get('id'),'result':result};code=200
            except Exception:response={'jsonrpc':'2.0','id':None,'error':{'code':-32000,'message':'Accepted publication import unavailable; inspect the private intent before retry'}};code=400
            raw=json.dumps(response).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',23120),Handler)
    threading.Thread(target=recover,name='accepted-import-recovery',daemon=True).start();server.serve_forever()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args();serve(args.config)
