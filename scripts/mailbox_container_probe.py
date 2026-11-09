"""Offline fixture executed inside a network-disabled, non-root container."""
import json
from pathlib import Path
import socket
import sys
import urllib.request

sys.path.insert(0,'/app/runtime')
from api_mailbox import MailboxClient,WorkerAPI

config=json.loads(Path('/fixture.json').read_text())
network_denied=False
try:
    with socket.create_connection(('1.1.1.1',443),timeout=1): pass
except OSError: network_denied=True
shim=WorkerAPI(MailboxClient('/ipc',config['credential'],1,timeout=20))
endpoint=shim.start()
try:
    request=urllib.request.Request(endpoint+'/chat/completions',
        json.dumps({'messages':[{'role':'user','content':'OFFLINE CONTAINER TRANSPORT FIXTURE'}]}).encode(),
        {'Authorization':'Bearer '+config['credential'],'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=25) as stream:response=json.load(stream)
finally:shim.close()
result={'networkDenied':network_denied,
        'fixtureReplyVerified':response=={'fixture':'received-by-trusted-offline-broker'},
        'workerLoopbackAdapter':True,'providerRequestSent':False}
Path('/work/result.json').write_text(json.dumps(result),encoding='utf-8')
if not all((result['networkDenied'],result['fixtureReplyVerified'])): raise SystemExit(1)
