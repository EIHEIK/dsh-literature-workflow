import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic
from user_authorization import locate_instruction,bind_existing_authorization,APPROVAL
import test_batches


class UserAuthorizationTests(unittest.TestCase):
    tearDown=test_batches.BatchTests.tearDown
    def setUp(self):
        test_batches.BatchTests.setUp(self)
        self.sessions=self.root/'sessions';self.sessions.mkdir();self.thread='01234567-1234-1234-1234-0123456789ab'
        self.log=self.sessions/('rollout-fixture-'+self.thread+'.jsonl')
        self.log.write_text(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':APPROVAL}},ensure_ascii=False)+'\n',encoding='utf-8')
        batch=self.store.batch(self.batch);job=self.store.get(self.job)
        job['zotero']={'parentItemKey':'ABCDEFGH'}
        batch['items'][0]['zotero']=job['zotero']
        # Build a fresh properly fingerprinted batch rather than editing its
        # summary after authorization.
        self.summary=self.store.prepare([{'source':str(self.source),'zotero':job['zotero']}],{'revision':'authorization-fixture'})
        self.batch=self.summary['batch'];job=self.store.get(self.summary['items'][0]['job'])
        self.intake=self.root/'intake.json';atomic(self.intake,{'ready':[{'sourceSha256':job['sourceSha256'],'pages':2,'zotero_key':'ABCDEFGH'}]})
        self.host={'thread':self.thread,'turn':'fixture','model':'gpt-6.1-sol','effort':'high','source':'fixture-host'}

    def bind(self):
        with patch('user_authorization.ModelGate.check',return_value=self.host):
            return bind_existing_authorization(self.store,self.batch,self.intake,self.sessions,self.thread)

    def test_actual_user_authorization_binds_frozen_summary_without_spending(self):
        result=self.bind();self.assertTrue(result['confirmed']);self.assertEqual(result['modelCalls'],0)
        self.assertEqual(result['summaryHash'],self.summary['summaryHash'])
        self.assertEqual(self.bind()['receiptHash'],result['receiptHash'])

    def test_assistant_quote_is_not_a_user_instruction(self):
        self.log.write_text(json.dumps({'type':'response_item','payload':{'role':'assistant','content':APPROVAL}})+'\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'actual user instruction'):self.bind()

    def test_native_response_item_user_format_is_supported(self):
        self.log.write_text(json.dumps({'type':'response_item','payload':{'role':'user','content':[{'type':'input_text','text':APPROVAL+'\n'}]}})+'\n',encoding='utf-8')
        self.assertTrue(self.bind()['confirmed'])

    def test_changed_source_inventory_or_budget_cannot_piggyback_authorization(self):
        atomic(self.intake,{'ready':[]})
        with self.assertRaisesRegex(ValueError,'outside'):self.bind()
