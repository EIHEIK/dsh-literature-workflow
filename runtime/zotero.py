"""Zotero local API reads + integrated MCP import. Durable per-file reconciliation journal."""
import json
from pathlib import Path
import re
import urllib.request
import urllib.parse
from core import atomic,digest,read_json

def local_url(value):
    u=urllib.parse.urlparse(value)
    if u.scheme!='http' or u.hostname not in ('127.0.0.1','localhost','::1') or u.username or u.password or u.query or u.fragment:
        raise ValueError('Only an explicitly configured loopback HTTP Zotero service is supported')
    return value.rstrip('/')

def key(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Z0-9]{8}',value):raise ValueError('Zotero key must be 8 uppercase letters/digits')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Zotero service redirects are not allowed')


def scope_title(job):
    pages=job['sourcePages']
    if pages==list(range(1,job['sourceInfo']['pages']+1)):return ''
    groups=[];start=last=pages[0]
    for number in pages[1:]+[None]:
        if number is not None and number==last+1:last=number;continue
        groups.append(str(start) if start==last else f'{start}–{last}')
        start=last=number
    return ' · 部分译本 p'+','.join(groups)

class Zotero:
    def __init__(self,api='http://127.0.0.1:23119/api',mcp='http://127.0.0.1:23120/mcp'):
        self.api=local_url(api);self.mcp=local_url(mcp);self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect());self.session=None;self.seq=0
    def get(self,path):
        with self.opener.open(self.api+path,timeout=30) as r: data=r.read().decode('utf-8')
        try:return json.loads(data)
        except json.JSONDecodeError:return data
    def children(self,parent):
        result=[];seen=set()
        for start in range(0,10000,100):
            items=self.get('/users/0/items/'+key(parent)+'/children?limit=100&start='+str(start))
            if not isinstance(items,list):raise ValueError('Invalid Zotero children response')
            for item in items:
                identity=item.get('key',item.get('data',{}).get('key'))
                if not identity or identity in seen:raise ValueError('Ambiguous Zotero pagination')
                seen.add(identity);result.append(item)
            if len(items)<100:return result
        raise ValueError('Zotero child inventory exceeded its explicit bound')
    def file(self,attachment):
        value=self.get('/users/0/items/'+key(attachment)+'/file/view/url')
        if not isinstance(value,str):raise ValueError('Zotero did not return a local attachment URL')
        u=urllib.parse.urlparse(value)
        if u.scheme!='file' or u.netloc not in ('','localhost'):raise ValueError('Attachment is not a local file')
        path=urllib.request.url2pathname(u.path)
        return str(Path(path).resolve(strict=True))
    def rpc(self,method,params=None,notification=False):
        self.seq+=1;obj={'jsonrpc':'2.0','method':method}
        if not notification:obj['id']=self.seq
        if params is not None:obj['params']=params
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2024-11-05'}
        if self.session:headers['Mcp-Session-Id']=self.session
        req=urllib.request.Request(self.mcp,json.dumps(obj).encode(),headers)
        with self.opener.open(req,timeout=90) as r:
            self.session=r.headers.get('Mcp-Session-Id',self.session);raw=r.read().decode('utf-8')
        if notification:return None
        if raw.lstrip().startswith('{'):reply=json.loads(raw)
        else:
            items=[json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith('data:') and line[5:].strip().startswith('{')]
            reply=next((v for v in items if v.get('id')==obj['id']),None)
        if not reply or reply.get('error'):raise ValueError('MCP request failed: '+method)
        result=reply.get('result',{})
        if result.get('isError'):raise ValueError('MCP tool reported an error: '+method)
        return result
    def connect(self):
        self.rpc('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'dsh-literature-workflow','version':'0.1.0-rc.1'}})
        self.rpc('notifications/initialized',notification=True)
        tools=self.rpc('tools/list').get('tools',[])
        tool=next((t for t in tools if t['name']=='write_item'),None)
        if not tool or not {'action','filePath','parentItemKey'}.issubset(tool.get('inputSchema',{}).get('properties',{})):
            raise ValueError('MCP write_item/import schema is incompatible; no import attempted')
        return tools
    def import_pdf(self,parent,path,title):
        return self.rpc('tools/call',{'name':'write_item','arguments':{'action':'import','filePath':path,'parentItemKey':key(parent),'title':title,'linkMode':'imported_file'}})

def archive(store,jid,a,client=None):
    j=store.resume(jid)
    if not j.get('publication') or not j.get('acceptance') or j['acceptance']!=store.acceptance(j):raise ValueError('Current accepted publication required')
    z=j.get('zotero') or {}; parent=key(z.get('parentItemKey'))
    if z.get('libraryType','user')!='user':raise ValueError('RC1 supports personal Zotero libraries only; group routing must be explicitly adapted')
    client=client or Zotero(a.get('zoteroApi','http://127.0.0.1:23119/api'),a.get('zoteroMcp','http://127.0.0.1:23120/mcp'))
    receipts=store.folder(j)/'archive'/j['publication']['id'];receipts.mkdir(parents=True,exist_ok=True)
    connected=False
    for mode,f in j['publication']['outputs'].items():
        title=('中文阅读版' if mode=='cn' else '中英左右对照')+scope_title(j)+' · '+j['candidate']['id'][-12:]
        path=receipts/(mode+'.json');record=read_json(path) if path.exists() else None
        matches=[]
        for item in client.children(parent):
            data=item.get('data',item)
            if data.get('title')==title and data.get('itemType')=='attachment' and data.get('parentItem')==parent:
                matches.append(item.get('key',data.get('key')))
        if len(matches)>1:raise ValueError('Ambiguous duplicate Zotero attachments; reconcile explicitly')
        if matches:
            local=client.file(matches[0])
            if digest(local)!=f['sha256']:raise ValueError('Existing Zotero attachment content does not match publication')
            record={'state':'verified','attachmentKey':matches[0],'parentItemKey':parent,'title':title,'sha256':f['sha256'],'publication':j['publication']['id']}
            atomic(path,record);j['archive'][mode]=record;continue
        if record:
            # A committed intent with no visible child is ambiguous (timeout/crash/remote lag).
            # Never blindly import twice; operator must reconcile the journal against Zotero.
            raise ValueError('Archive intent exists but attachment cannot be found; needs reconciliation, no duplicate import attempted')
        if not connected:client.connect();connected=True
        atomic(path,{'state':'intent','title':title,'sha256':f['sha256'],'parentItemKey':parent,'publication':j['publication']['id']})
        client.import_pdf(parent,f['path'],title)
        matches=[]
        for item in client.children(parent):
            d=item.get('data',item)
            if d.get('title')==title and d.get('parentItem')==parent and d.get('itemType')=='attachment':matches.append(item.get('key',d.get('key')))
        if len(matches)!=1 or digest(client.file(matches[0]))!=f['sha256']:raise ValueError('Zotero import needs reconciliation; cannot verify copied bytes')
        record={'state':'verified','attachmentKey':matches[0],'parentItemKey':parent,'title':title,'sha256':f['sha256'],'publication':j['publication']['id']}
        atomic(path,record);j['archive'][mode]=record
    j['state']='archived';store.save(j);return j
