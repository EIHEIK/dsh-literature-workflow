"""Request Zotero's own user dialog once; write its key privately, never stdout.

Run only after the user chooses the official local write route. This does not
import any attachment or grant the translation worker new access.
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.request

from zotero_native_bridge import NoRedirect


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--service-config',required=True);args=parser.parse_args()
    service=Path(args.service_config).resolve(strict=True);destination=service.parent/'zotero-bridge.json'
    if destination.exists():raise ValueError('Existing native authorization is preserved; inspect it before requesting another dialog')
    config=json.loads(service.read_text(encoding='utf-8'));sys.path.insert(0,config['runtime'])
    from core import atomic
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open('http://127.0.0.1:23119/api/',timeout=10) as response:server=response.headers.get('Zotero-Server-ID')
    if not server:raise ValueError('Zotero 10 server identity is unavailable')
    request=urllib.request.Request('http://127.0.0.1:23119/api/local/authorize',
        json.dumps({'appName':'Literature Workflow — 仅回挂本批已验收译本'}).encode(),
        {'Content-Type':'application/json','Zotero-Server-ID':server},method='POST')
    print('Zotero authorization dialog requested; waiting for the user. No attachment writes.',flush=True)
    with opener.open(request,timeout=600) as response:
        raw=response.read(16385)
    if len(raw)>16384:raise ValueError('Unexpected authorization response size')
    result=json.loads(raw)
    if result.get('remember') is not True:raise ValueError('A single-use grant cannot support unattended imports; no key was persisted')
    key=result.get('key')
    if not isinstance(key,str) or len(key)!=32:raise ValueError('Unexpected authorization key format')
    atomic(destination,{'serviceConfig':str(service),'serverID':server,'key':key,'remember':True,
                        'policy':'Only current accepted workflow publication hashes and registered parents'})
    print(json.dumps({'authorized':True,'config':str(destination),'keyPrinted':False,'attachmentWrites':0},ensure_ascii=False))
