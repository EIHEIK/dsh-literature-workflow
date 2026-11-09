"""Narrow current-chat adapter; no confirmation minting or model invocation."""
import argparse
import json
import os
from urllib.parse import urlparse
import urllib.request

from control_api import ControlAPI
from core import read_json
from model_gate import ModelGate
from provider_http import NoRedirect


def call(config_path,method,arguments):
    if method not in ControlAPI.METHODS:raise ValueError('Unsupported control operation')
    config=read_json(config_path);url=urlparse(config['url'])
    if set(config)!={'url','codexToken','sessionsRoot'} or url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path not in ('','/') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Expected the private local Codex adapter configuration')
    thread=os.environ.get('CODEX_THREAD_ID')
    evidence=ModelGate(config['sessionsRoot'],expected_effort=None).check(thread)
    payload=json.dumps({'method':method,'arguments':arguments}).encode()
    if len(payload)>65536:raise ValueError('Control request is too large')
    request=urllib.request.Request(config['url'].rstrip('/')+'/rpc',payload,
        {'Content-Type':'application/json','Authorization':'Bearer '+config['codexToken'],'X-Literature-Host-Thread':thread})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=35) as response:raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('Controller response is too large')
    return {'host':evidence,**json.loads(raw)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('method',choices=ControlAPI.METHODS);a=p.parse_args()
    # Structured JSON on stdin prevents shell quoting of user-selected scope.
    import sys
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('Input too large')
        print(json.dumps(call(a.config,a.method,json.loads(raw or b'{}')),ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':type(error).__name__,'message':'Codex host verification or local operation failed; inspect readiness and existing state'}))
        raise SystemExit(1)
