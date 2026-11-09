import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from proxy import Meter

class ProxyTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.test-output';base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base);self.log=Path(self.tmp.name)/'usage.jsonl'
    def tearDown(self):self.tmp.cleanup()
    def test_usage_cap_and_no_secret_or_prompt_persisted(self):
        m=Meter('synthetic-key','test-model',self.log,limit=1,qps=1000)
        response={'id':'synthetic-response','choices':[{'finish_reason':'stop','message':{'content':'translated'}}],'usage':{'prompt_tokens':10,'completion_tokens':3}}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as call:
            m.request(json.dumps({'messages':[{'role':'user','content':'PRIVATE-PROMPT'}]}).encode())
            self.assertEqual(call.call_args.args[0].headers['Authorization'],'Bearer synthetic-key')
            with self.assertRaisesRegex(ValueError,'cap'):m.request(b'{}')
        text=self.log.read_text();self.assertNotIn('synthetic-key',text);self.assertNotIn('PRIVATE-PROMPT',text)
        self.assertEqual(json.loads(text)['usage']['completion_tokens'],3)
    def test_truncation_fails_but_records_usage(self):
        m=Meter('synthetic','test',self.log)
        response={'choices':[{'finish_reason':'length','message':{'content':'partial'}}],'usage':{'completion_tokens':20}}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(ValueError,'truncated'):m.request(b'{}')
        row=json.loads(self.log.read_text());self.assertEqual(row['status'],'failed');self.assertEqual(row['usage']['completion_tokens'],20)
    def test_unreported_failure_is_not_zero_usage(self):
        m=Meter('synthetic','test',self.log)
        with patch('urllib.request.urlopen',side_effect=TimeoutError('synthetic')):
            with self.assertRaises(TimeoutError):m.request(b'{}')
        self.assertIsNone(json.loads(self.log.read_text())['usage'])

if __name__=='__main__':unittest.main()
