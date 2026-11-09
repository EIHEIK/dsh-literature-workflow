"""Bounded JSON request transport across a network-disabled worker boundary.

Only a per-generation mailbox directory is shared. No command strings, URLs,
host file paths or real provider credentials are part of the protocol. Provider
policy, persistent billing and network requests belong to the trusted broker.

Deployment MUST mount this directory as its own mount root, outside any other
writable worker mount. Its parent must stay controller-only, so the worker cannot
replace/rename the directory while the controller accesses it.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from core import atomic, canonical, object_hash

MAX_REQUEST_BYTES = 8*1024*1024
MAX_RESPONSE_BYTES = 16*1024*1024


def read_bounded(path, root, maximum):
    path = Path(path)
    if path.is_symlink(): raise ValueError('Mailbox links are forbidden')
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root): raise ValueError('Mailbox path escaped its generation')
    if resolved.parent != root: raise ValueError('Nested mailbox paths are forbidden')
    # Open the final component without following reparse points/symlinks, avoiding
    # the check-then-open race with a worker that can replace directory entries.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW
        create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype=wintypes.HANDLE
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE]
        handle=create(str(resolved),0x80000000,7,None,3,0x00200000,None)  # OPEN_REPARSE_POINT
        if handle==wintypes.HANDLE(-1).value: raise OSError(ctypes.get_last_error(),'Cannot open mailbox file')
        class AttributeTag(ctypes.Structure):
            _fields_=[('attributes',wintypes.DWORD),('tag',wintypes.DWORD)]
        info=AttributeTag()
        inspect=kernel.GetFileInformationByHandleEx
        inspect.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
        if not inspect(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or info.attributes & 0x400:
            close(handle);raise ValueError('Mailbox reparse points are forbidden')
        try: descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except BaseException: close(handle);raise
    else:
        descriptor=os.open(resolved,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('Mailbox object is not a regular file')
        raw=stream.read(maximum+1)
    if len(raw)>maximum: raise ValueError('Mailbox object exceeds size limit')
    value = json.loads(raw)
    if not isinstance(value,dict): raise ValueError('Mailbox object must be an object')
    return value


class MailboxClient:
    def __init__(self, directory, credential, generation, *, timeout=180):
        self.root = Path(directory).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.credential=credential; self.generation=generation; self.timeout=timeout

    def exchange(self, body):
        if not isinstance(body,dict) or len(canonical(body).encode('utf-8'))>MAX_REQUEST_BYTES-1024:
            raise ValueError('Model request exceeds mailbox limit')
        request_id = uuid.uuid4().hex
        request_hash = object_hash(body)
        request = {'version':1,'id':request_id,'generation':self.generation,
                   'credential':self.credential,'sha256':request_hash,'body':body}
        atomic(self.root/(request_id+'.request.json'),request)
        response_path = self.root/(request_id+'.response.json')
        deadline = time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            if response_path.exists():
                response = read_bounded(response_path,self.root,MAX_RESPONSE_BYTES)
                if (response.get('version')!=1 or response.get('id')!=request_id or
                        response.get('generation')!=self.generation or response.get('requestSha256')!=request_hash):
                    raise ValueError('Mailbox response identity mismatch')
                if 'error' in response: raise RuntimeError('Model broker rejected or could not complete request')
                if not isinstance(response.get('body'),dict): raise ValueError('Invalid broker response')
                return response['body']
            time.sleep(.05)
        # Nothing is refunded or deleted: dispatch/usage may already have happened.
        raise TimeoutError('Model request outcome unknown; preserve its budget reservation')


class MailboxInbox:
    def __init__(self,directory,credential,generation):
        self.root=Path(directory).resolve(strict=True)
        if not isinstance(credential,str) or len(credential)<32 or type(generation) is not int or generation<1:
            raise ValueError('Scoped mailbox credential and generation required')
        self.credential=credential; self.generation=generation

    def pending(self):
        requests=[]
        paths=list(self.root.glob('*.request.json'))
        if len(paths)>1000: raise ValueError('Worker mailbox request count exceeded')
        for path in sorted(paths):
            request_id=path.name.removesuffix('.request.json')
            if not re.fullmatch('[a-f0-9]{32}',request_id): raise ValueError('Invalid mailbox request identifier')
            request=read_bounded(path,self.root,MAX_REQUEST_BYTES)
            if (set(request)!={'version','id','generation','credential','sha256','body'} or request['version']!=1 or
                    request['id']!=request_id or request['generation']!=self.generation or
                    not isinstance(request['credential'],str) or
                    not hmac.compare_digest(request['credential'],self.credential) or
                    not isinstance(request['body'],dict) or object_hash(request['body'])!=request['sha256']):
                raise ValueError('Mailbox scope, credential or request hash mismatch')
            response=self.root/(request_id+'.response.json')
            if response.exists():
                old=read_bounded(response,self.root,MAX_RESPONSE_BYTES)
                if old.get('requestSha256')!=request['sha256'] or old.get('generation')!=self.generation:
                    raise ValueError('Existing mailbox response has another identity')
                # This is transport idempotency only. The broker's durable ledger
                # must additionally verify its own response receipt, because the
                # worker can write this directory and cannot certify provider use.
                continue
            requests.append({'id':request_id,'generation':self.generation,'sha256':request['sha256'],'body':request['body']})
        return requests

    def complete(self,request,*,body=None,error=None):
        if (request['generation']!=self.generation or not re.fullmatch('[a-f0-9]{32}',request['id']) or
                object_hash(request['body'])!=request['sha256']): raise ValueError('Response does not match request')
        result={'version':1,'id':request['id'],'generation':self.generation,'requestSha256':request['sha256']}
        if error is not None:
            # Don't reflect upstream text, endpoint URLs, or credentials.
            result['error']={'kind':'broker_failure'}
        elif isinstance(body,dict): result['body']=body
        else: raise ValueError('Response object or error required')
        if len(canonical(result).encode('utf-8'))>MAX_RESPONSE_BYTES: raise ValueError('Broker response too large')
        if self.root.is_symlink() or self.root.resolve(strict=True)!=self.root:
            raise ValueError('Mailbox mount root changed')
        atomic(self.root/(request['id']+'.response.json'),result)
        return hashlib.sha256(canonical(result).encode('utf-8')).hexdigest()


class WorkerAPI:
    """OpenAI-compatible loopback shim inside a network-none container.

    The shim only transports messages. It cannot choose a real provider, access
    its credentials, modify billing, or accept a publication.
    """
    def __init__(self, mailbox_client): self.client=mailbox_client

    def start(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        client=self.client
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def setup(self):
                super().setup();self.connection.settimeout(client.timeout+5)
            def do_POST(self):
                try:
                    if self.path not in ('/chat/completions','/v1/chat/completions'):
                        raise ValueError('Unsupported model path')
                    if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+client.credential):
                        raise PermissionError('Invalid worker credential')
                    if self.headers.get('Origin') is not None or self.headers.get('Transfer-Encoding') is not None:
                        raise ValueError('Unsupported request framing')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<MAX_REQUEST_BYTES-1024:raise ValueError('Invalid request size')
                    raw=self.rfile.read(size)
                    if len(raw)!=size:raise ValueError('Incomplete request')
                    result=client.exchange(json.loads(raw));code=200
                except Exception:
                    result={'error':{'message':'Scoped model broker unavailable or request rejected','type':'broker_failure'}};code=502
                data=json.dumps(result,ensure_ascii=False).encode('utf-8')
                self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers()
                try:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return 'http://127.0.0.1:'+str(self.server.server_port)+'/v1'

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
