from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from dot_mcp import DotMCP


class ClientFixture:
    def __init__(self): self.confirmations=[]; self.calls=[]; self.confirmed=False
    def call(self, method, arguments):
        self.calls.append((method, arguments))
        if method=='get_capabilities': return {'productionReady':True}
        if method=='get_status':
            return {'batch':'fixture','summaryHash':'hash','confirmed':self.confirmed,
                    'items':[{'title':'Synthetic paper','pages':[1,2],'job':'paper-fixture','budget':{'cap':10000000}}],
                    'pilotBudget':{'cap':15000000},'batchCap':None}
        return {'state':'running'}
    def confirm(self, *args): self.confirmations.append(args); self.confirmed=True


class DotMCPTests(unittest.TestCase):
    def setUp(self):
        self.client=ClientFixture(); self.allowed=True
        def gate():
            if not self.allowed: raise PermissionError('wrong model')
            return {'model':'gpt-6.1-sol','effort':'medium','source':'fixture-only'}
        self.server=DotMCP(self.client,gate)
        self.server.process({'jsonrpc':'2.0','id':0,'method':'initialize','params':{'capabilities':{'elicitation':{}}}})
        self.server.process({'jsonrpc':'2.0','method':'notifications/initialized'})
    def call(self, name='start_batch', arguments=None):
        return self.server.process({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':name,'arguments':{'batch':'fixture','summaryHash':'hash'} if arguments is None else arguments}})
    def reply(self, prompt, action='accept', confirm=True):
        return self.server.process({'jsonrpc':'2.0','id':prompt['id'],'result':{'action':action,'content':{'confirm':confirm}}})
    def test_start_requires_real_correlated_elicitation_response(self):
        prompt=self.call()[0]
        self.assertEqual(prompt['method'],'elicitation/create')
        self.assertEqual(self.client.confirmations,[])
        self.assertFalse(any(m=='start_batch' for m,a in self.client.calls))
        self.reply({'id':'forged'})
        self.assertEqual(self.client.confirmations,[])
        self.assertFalse(self.reply(prompt)[0]['result']['isError'])
        self.assertEqual(len(self.client.confirmations),1)
        self.assertEqual(self.reply(prompt),[])
        self.assertEqual(len(self.client.confirmations),1)
    def test_decline_and_client_cancellation_cannot_start(self):
        prompt=self.call()[0]; self.reply(prompt,action='decline')
        self.assertEqual(self.client.confirmations,[])
        prompt=self.call()[0]
        self.server.process({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':1}})
        self.assertEqual(self.reply(prompt),[])
        self.assertEqual(self.client.confirmations,[])
    def test_model_change_during_confirmation_blocks_start(self):
        prompt=self.call()[0]; self.allowed=False
        self.assertTrue(self.reply(prompt)[0]['result']['isError'])
        self.assertEqual(self.client.confirmations,[])
    def test_no_self_approval_argument_or_missing_host_capability(self):
        self.assertTrue(self.call(arguments={'batch':'fixture','summaryHash':'hash','confirm':True})[0]['result']['isError'])
        self.server.elicitation=False
        self.assertTrue(self.call()[0]['result']['isError'])
        self.assertEqual(self.client.confirmations,[])
    def test_confirmed_repeat_is_idempotent_without_another_prompt(self):
        self.client.confirmed=True
        self.assertFalse(self.call()[0]['result']['isError'])
        self.assertEqual(self.server.pending,{})
    def test_wrong_model_cannot_read_or_mutate_jobs(self):
        self.allowed=False
        for name,args in [('preflight',{}),('get_status',{'batch':'fixture'}),('cancel_batch',{'batch':'fixture'})]:
            self.assertTrue(self.call(name,args)[0]['result']['isError'])
        self.assertEqual(self.client.calls,[])

    def test_real_stdio_handshake_lists_tools_and_fails_closed_without_host(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'.test-output') as directory:
            config=Path(directory)/'fixture-config.json'
            config.write_text(json.dumps({'url':'http://127.0.0.1:1','dotToken':'d'*44,
                'confirmationToken':'c'*44,'sessionsRoot':directory}), encoding='utf-8')
            messages=[{'jsonrpc':'2.0','id':0,'method':'initialize','params':{'capabilities':{}}},
                      {'jsonrpc':'2.0','method':'notifications/initialized'},
                      {'jsonrpc':'2.0','id':1,'method':'tools/list'},
                      {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'preflight','arguments':{}}}]
            result=subprocess.run([sys.executable,str(root/'runtime/dot_mcp.py'),'--config',str(config)],
                input=('\n'.join(json.dumps(m) for m in messages)+'\n').encode(),capture_output=True,timeout=10,
                env={**os.environ,'CODEX_THREAD_ID':'invalid-host','PYTHONIOENCODING':'cp936'})
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            replies=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([m['id'] for m in replies],[0,1,2])
            self.assertIn('start_batch',[t['name'] for t in replies[1]['result']['tools']])
            self.assertTrue(replies[2]['result']['isError'])


if __name__ == '__main__': unittest.main()
