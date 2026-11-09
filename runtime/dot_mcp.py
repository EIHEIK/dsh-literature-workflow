"""Small stdio MCP adapter with host-mediated, batch-bound user confirmation.

Protocol: MCP 2025-06-18 (newline UTF-8 JSON-RPC). No model invocation, arbitrary
shell, remote MCP gateway or auto-approval. stdout contains protocol messages
only. Deployment config and host thread identity are outside tool arguments.
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse
import urllib.request
import uuid

from control_api import fields
from model_gate import ModelGate


def schema(properties, required=()):
    return {'type':'object', 'properties':properties, 'required':list(required), 'additionalProperties':False}


def tools():
    string = {'type':'string'}
    batch = schema({'batch':string}, ('batch',))
    scope=schema({'kind':{'enum':['items','collection']},
                  'keys':{'type':'array','items':{'type':'string','pattern':'^[A-Z0-9]{8}$'},'minItems':1,'maxItems':100}},('kind','keys'))
    inputs = {
        'preflight': schema({}),
        'refresh_index':schema({'scope':scope,'requestId':{'type':'string','pattern':'^[a-zA-Z0-9_-]{16,64}$'}},('scope','requestId')),
        'get_index_status':schema({'index':string},('index',)),
        'prepare_batch': schema({'scope':schema({'kind':{'enum':['items','collection']},
                           'keys':{'type':'array','items':{'type':'string','pattern':'^[A-Z0-9]{8}$'},'minItems':1,'maxItems':100}}, ('kind','keys')),
                           'pages':string, 'title':string}, ('scope',)),
        'start_batch': schema({'batch':string,'summaryHash':string}, ('batch','summaryHash')),
        'get_status':batch, 'pause_batch':batch, 'resume_batch':batch, 'cancel_batch':batch,
        'get_delivery':batch,
        'read_events':schema({'after':{'type':'integer','minimum':0},'batch':string,'limit':{'type':'integer','minimum':1,'maximum':100}}),
    }
    descriptions = {
        'preflight':'Verify actual local host Work model/effort before any batch operation. No model requests.',
        'refresh_index':'Queue unpaid offline extraction of selected Zotero papers; reuse requestId after a lost response. No translation or Zotero writes.',
        'get_index_status':'Read durable indexing progress and per-paper omissions before preparing a batch.',
        'prepare_batch':'Prepare frozen Zotero inputs and budget summary. No paid translation before user confirmation.',
        'start_batch':'Start the exact summary version after host-mediated user confirmation. Cannot self-approve.',
        'get_status':'Read persisted batch progress, costs and blockers.',
        'read_events':'Read incremental persisted events using a cursor.',
        'pause_batch':'Stop new dispatch; retain in-flight checkpoints and costs.',
        'resume_batch':'Resume a paused batch without resetting costs, failures or acceptance.',
        'cancel_batch':'Cancel the batch and terminate its workers; preserve all original and prior outputs.',
        'get_delivery':'Return hash-verified accepted publications and Zotero receipts.',
    }
    return [{'name':name,'description':descriptions[name],'inputSchema':value,
             'annotations':{'readOnlyHint':name in ('preflight','get_status','read_events','get_delivery','get_index_status'),
                            'destructiveHint':name=='cancel_batch','openWorldHint':False}}
            for name,value in inputs.items()]


class ControlClient:
    def __init__(self, config, thread_id):
        parsed = urlparse(config['url'])
        if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or
                parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Only the provisioned loopback controller is supported')
        self.url = config['url'].rstrip('/')
        self.token = config['dotToken']; self.confirmation_token = config['confirmationToken']
        if any(not isinstance(t,str) or len(t)<40 for t in (self.token,self.confirmation_token)):
            raise ValueError('Provisioned adapter credentials required')
        self.thread = thread_id
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, endpoint, payload, token):
        request = urllib.request.Request(self.url+endpoint, json.dumps(payload).encode('utf-8'),
            {'Content-Type':'application/json','Authorization':'Bearer '+token,
             'X-Literature-Host-Thread':self.thread})
        with self.opener.open(request, timeout=35) as response:
            raw = response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024: raise ValueError('Controller response too large')
        result = json.loads(raw)
        if 'result' not in result: raise ValueError('Controller rejected request')
        return result['result']

    def call(self, method, arguments):
        return self.post('/rpc', {'method':method,'arguments':arguments}, self.token)

    def confirm(self, batch, summary_hash, receipt):
        return self.post('/confirmation', {'batch':batch,'summaryHash':summary_hash,'receipt':receipt}, self.confirmation_token)


class DotMCP:
    def __init__(self, client, gate):
        self.client = client; self.gate = gate
        self.initialized = False; self.ready = False; self.elicitation = False
        self.pending = {}

    def result(self, request_id, value, *, error=False):
        return {'jsonrpc':'2.0','id':request_id,'result':{'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}], 'isError':error}}

    def process(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc')!='2.0':
            return [{'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid JSON-RPC request'}}]
        request_id = message.get('id'); method = message.get('method')
        if method is None:
            pending = self.pending.pop(request_id, None)
            if not pending: return []  # Unsolicited/replayed confirmation cannot start a batch.
            result = message.get('result', {})
            if (time.time()>pending['expires'] or result.get('action')!='accept' or
                    result.get('content',{}).get('confirm') is not True):
                return [self.result(pending['request'], {'state':'not_started','reason':'User declined, cancelled or confirmation expired'})]
            try:
                self.gate()
                self.client.confirm(pending['batch'], pending['summaryHash'], 'mcp-elicitation:'+str(request_id))
                value = self.client.call('start_batch', {'batch':pending['batch'],'summaryHash':pending['summaryHash']})
                return [self.result(pending['request'], value)]
            except Exception:
                return [self.result(pending['request'], {'error':'Start failed; inspect current status before retrying'}, error=True)]
        params = message.get('params', {})
        if method == 'initialize':
            if self.initialized:
                return [{'jsonrpc':'2.0','id':request_id,'error':{'code':-32600,'message':'Already initialized'}}]
            self.initialized = True
            self.elicitation = 'elicitation' in params.get('capabilities', {})
            return [{'jsonrpc':'2.0','id':request_id,'result':{'protocolVersion':'2025-06-18',
                    'capabilities':{'tools':{}},'serverInfo':{'name':'literature-workflow-local','version':'0.2.0-dev'}}}]
        if method == 'notifications/initialized':
            self.ready = self.initialized; return []
        if method == 'notifications/cancelled':
            for key, value in list(self.pending.items()):
                if value['request']==params.get('requestId'): self.pending.pop(key)
            return []
        if request_id is None: return []
        if method == 'ping': return [{'jsonrpc':'2.0','id':request_id,'result':{}}]
        if not self.ready:
            return [{'jsonrpc':'2.0','id':request_id,'error':{'code':-32000,'message':'Initialize first'}}]
        if method == 'tools/list': return [{'jsonrpc':'2.0','id':request_id,'result':{'tools':tools()}}]
        if method != 'tools/call':
            return [{'jsonrpc':'2.0','id':request_id,'error':{'code':-32601,'message':'Method not supported'}}]
        try:
            name = params.get('name'); args = params.get('arguments', {})
            known = {entry['name']:entry for entry in tools()}
            if name not in known: raise ValueError('Unknown tool')
            shape = known[name]['inputSchema']
            fields(args, shape['required'], shape['properties'])
            evidence = self.gate()
            if name == 'preflight':
                capabilities = self.client.call('get_capabilities', {})
                return [self.result(request_id, {'hostVerified':True,'evidence':evidence, **capabilities})]
            if name == 'start_batch':
                capabilities=self.client.call('get_capabilities', {})
                if capabilities.get('productionReady') is not True and capabilities.get('pilotReady') is not True:
                    raise ValueError('Production deployment not ready')
                status = self.client.call('get_status', {'batch':args['batch']})
                if status.get('startAllowed',capabilities.get('productionReady')) is not True:
                    raise ValueError('This batch is outside the validated deployment scope')
                if status['summaryHash']!=args['summaryHash']: raise ValueError('Summary changed')
                if not status['confirmed']:
                    if not self.elicitation: raise ValueError('Host user confirmation capability unavailable')
                    if self.pending: raise ValueError('A batch confirmation is already pending')
                    elicitation_id = 'confirm-'+uuid.uuid4().hex
                    self.pending[elicitation_id] = {'request':request_id,'batch':args['batch'],
                        'summaryHash':args['summaryHash'],'expires':time.time()+900}
                    summary = {'batch':status['batch'],'summaryHash':status['summaryHash'],
                               'items':[{'title':item['title'],'sourcePages':item['pages'],
                                         'job':item['job'],'budget':item['budget']} for item in status['items']],
                               'pilotBudget':status['pilotBudget'],'batchCap':status['batchCap']}
                    return [{'jsonrpc':'2.0','id':elicitation_id,'method':'elicitation/create','params':{
                        'message':'确认后启动此批文献翻译。费用单位为 micro-CNY（1000000 = 1 元）；单篇 5 元提醒、10 元硬上限；试点累计 15 元。\n'+json.dumps(summary,ensure_ascii=False),
                        'requestedSchema':schema({'confirm':{'type':'boolean','title':'确认此范围与预算并启动','default':False}}, ('confirm',))}}]
            return [self.result(request_id, self.client.call(name, args))]
        except PermissionError:
            return [self.result(request_id, {'error':'Host verification failed: actual GPT-6.1 Sol / medium and a current trusted local Work record are required'}, error=True)]
        except Exception:
            return [self.result(request_id, {'error':'Operation rejected or unavailable; inspect status, capabilities and the exact summary version'}, error=True)]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True)
    options = parser.parse_args()
    with open(options.config, encoding='utf-8') as stream: config = json.load(stream)
    thread = os.environ.get('CODEX_THREAD_ID')
    gate = ModelGate(config['sessionsRoot'])
    server = DotMCP(ControlClient(config, thread), lambda:gate.check(thread))
    while True:
        raw = sys.stdin.buffer.readline(65537)
        if not raw: break
        if len(raw)>65536 or not raw.endswith(b'\n'): break
        try: responses = server.process(json.loads(raw))
        except Exception:
            responses = [{'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid MCP input'}}]
        for response in responses:
            sys.stdout.buffer.write((json.dumps(response,ensure_ascii=False)+'\n').encode('utf-8'))
            sys.stdout.buffer.flush()


if __name__ == '__main__': main()
