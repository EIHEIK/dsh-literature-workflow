"""Loopback-only API metering proxy. Real credentials never enter CLI args or jobs."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import secrets
import threading
import time
import urllib.request
import urllib.error

class Meter:
    def __init__(self,key,model,path,limit=100,qps=1,concurrency=2):
        self.key=key; self.model=model; self.path=path; self.limit=limit; self.qps=qps
        self.token=secrets.token_urlsafe(32);self.lock=threading.Lock();self.sem=threading.Semaphore(concurrency)
        self.count=0;self.last=0;self.stopped=False
    def record(self,value):
        with self.lock:
            with open(self.path,'a',encoding='utf-8') as f: f.write(json.dumps(value,ensure_ascii=False)+'\n');f.flush()
    def request(self,body):
        with self.sem:
            with self.lock:
                if self.stopped or self.count>=self.limit: raise ValueError('Request cap reached or worker stopped')
                self.count+=1;seq=self.count;delay=max(0,self.last+1/self.qps-time.monotonic());self.last=time.monotonic()+delay
            time.sleep(delay)
            if self.stopped: raise ValueError('Worker stopped')
            obj=json.loads(body);obj['model']=self.model;obj['stream']=False
            encoded=json.dumps(obj).encode();start=time.time()
            row={'sequence':seq,'startedAt':start,'requestSha256':hashlib.sha256(encoded).hexdigest(),'model':self.model,'usage':None}
            try:
                req=urllib.request.Request('https://api.deepseek.com/chat/completions',encoded,
                      {'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=180) as response: data=response.read(32*1024*1024)
                parsed=json.loads(data);row['usage']=parsed.get('usage');row['responseId']=parsed.get('id')
                choice=(parsed.get('choices') or [{}])[0]
                if choice.get('finish_reason')!='stop' or not choice.get('message',{}).get('content','').strip():
                    raise ValueError('Empty or truncated translation; do not count as success')
                row['status']='completed';return data
            except Exception as e:
                row['status']='failed';row['errorType']=type(e).__name__;raise
            finally:
                row['elapsedSeconds']=time.time()-start;self.record(row)
    def start(self):
        meter=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                try:
                    if self.path not in ('/chat/completions','/v1/chat/completions'): raise ValueError('Unsupported API path')
                    if self.headers.get('Authorization')!='Bearer '+meter.token: raise ValueError('Invalid local proxy credential')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=8*1024*1024: raise ValueError('Invalid body size')
                    data=meter.request(self.rfile.read(size));code=200
                except Exception as e:
                    # Deliberately exclude response bodies/URLs and credential-bearing exception messages.
                    data=json.dumps({'error':{'message':'Translation upstream failed or budget exhausted','type':type(e).__name__}}).encode();code=502
                self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers()
                try:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self):
        self.stopped=True;self.server.shutdown();self.server.server_close()
