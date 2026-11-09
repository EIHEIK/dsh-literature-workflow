from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic,code_hash,digest,object_hash
from deployment_policy import DeploymentPolicy
from control_api import ControlAPI
import test_batches


class DeploymentPolicyTests(unittest.TestCase):
    tearDown=test_batches.BatchTests.tearDown
    start=test_batches.BatchTests.start
    def setUp(self):
        test_batches.BatchTests.setUp(self);self.image=self.root/'image.json';atomic(self.image,{'fixture':True})
        self.receipt=self.root/'pilot.json'
        self.config={'paidWorkEnabled':True,'imageLock':str(self.image),'pilotReadinessReceipt':str(self.receipt),
                     'readinessReceipt':str(self.root/'production.json')}
        self.body={'mode':'pilot','runtimeHash':code_hash(),'imageLockSha256':digest(self.image),'expiresAt':time.time()+3600,
                   'offlinePipelinePassed':True,'dotHostVerified':True,'confirmationVerified':True,
                   'allowedSummaryHashes':[self.summary['summaryHash']]}
        atomic(self.receipt,self.body);self.policy=DeploymentPolicy(self.config)

    def test_pilot_can_start_only_exact_summary_after_user_confirmation(self):
        api=ControlAPI(None,{},readiness=self.policy.production_ready,pilot_readiness=self.policy.pilot_ready,batch_authorizer=self.policy.allows)
        self.assertFalse(api.call(self.store,'get_capabilities',{})['productionReady'])
        self.assertTrue(api.call(self.store,'get_capabilities',{})['pilotReady'])
        with self.assertRaisesRegex(ValueError,'confirmation'):
            api.call(self.store,'start_batch',{'batch':self.batch,'summaryHash':self.summary['summaryHash']})
        self.store.confirm(self.batch,self.summary['summaryHash'],'fixture-human')
        self.assertEqual(api.call(self.store,'start_batch',{'batch':self.batch,'summaryHash':self.summary['summaryHash']})['state'],'running')
        changed=self.store.prepare(self.inputs,{'revision':'different'},pilot=True)
        self.assertFalse(self.policy.allows(self.store,changed['batch']))

    def test_pilot_gate_requires_prerequisites_runtime_scope_and_budget(self):
        for mutation in ({'dotHostVerified':False},{'expiresAt':time.time()-1},{'runtimeHash':'0'*64},
                         {'allowedSummaryHashes':[]},{'confirmationVerified':False}):
            atomic(self.receipt,{**self.body,**mutation});self.assertFalse(self.policy.allows(self.store,self.batch))
        atomic(self.receipt,self.body)
        batch=self.store.batch(self.batch);batch['pilot']=False
        with self.store.transaction():self.store.save_batch(batch)
        self.assertFalse(self.policy.allows(self.store,self.batch))
        self.config['paidWorkEnabled']=False;self.assertFalse(self.policy.enabled())

    def test_expiry_pauses_running_batch_without_resetting_budget(self):
        self.start();before=self.store.budgets.status('pilot:first')
        atomic(self.receipt,{**self.body,'expiresAt':time.time()-1})
        self.policy.guard(self.store)
        self.assertEqual(self.store.batch(self.batch)['state'],'paused')
        self.assertEqual(self.store.budgets.status('pilot:first'),before)

    def test_codex_supervision_does_not_require_dot_but_requires_its_own_evidence(self):
        self.config['supervisionMode']='codex'
        self.assertFalse(self.policy.pilot_ready())
        value={**self.body,'dotHostVerified':False,'codexHostVerified':True}
        atomic(self.receipt,value)
        self.assertTrue(self.policy.pilot_ready());self.assertFalse(self.policy.production_ready())
        production={**value,'codexEndToEnd':True,'productionBackend':True,'realPilotAccepted':True}
        atomic(self.config['readinessReceipt'],production)
        self.assertTrue(self.policy.production_ready())

    def scoped_fixture(self):
        self.config.update(supervisionMode='codex',data=str(self.store.root),scopedBatchReadinessReceipt=str(self.root/'scoped.json'))
        summary=self.store.prepare(self.inputs,{'revision':'bounded-synthetic'},pilot=False)
        batch=self.store.batch(summary['batch'])
        body={'batch':batch['id'],'summaryHash':batch['summaryHash'],'runtimeHash':code_hash(),'items':batch['items']}
        auth_path=self.store.root/'user-authorizations'/'synthetic.json';atomic(auth_path,body);auth_hash=object_hash(body)
        self.store.confirm(batch['id'],batch['summaryHash'],'codex-user-instruction:'+auth_hash)
        package=self.store.root/'jobs'/('paper-'+'f'*16)/'problem-packages'/'fixture.zip';package.parent.mkdir(parents=True);package.write_bytes(b'SYNTHETIC ISSUE ONLY')
        receipt={'mode':'original-list-bounded','runtimeHash':code_hash(),'imageLockSha256':digest(self.image),
            'expiresAt':time.time()+3600,'offlinePipelinePassed':True,'codexHostVerified':True,'confirmationVerified':True,'scopeVerified':True,
            'allowedBatch':{'id':batch['id'],'summaryHash':batch['summaryHash'],'sourceHashes':[r['sourceSha256'] for r in batch['items']]},
            'authorization':{'receipt':str(auth_path),'receiptHash':auth_hash},
            'deferredPilot':{'job':'paper-'+'f'*16,'sourceSha256':'f'*64,'problemPackage':{'path':str(package),'sha256':digest(package)}}}
        atomic(self.config['scopedBatchReadinessReceipt'],receipt);return batch,receipt

    def test_bounded_original_batch_does_not_assert_general_production_readiness(self):
        batch,receipt=self.scoped_fixture()
        self.assertTrue(self.policy.enabled());self.assertTrue(self.policy.scoped_batch_ready())
        self.assertFalse(self.policy.production_ready());self.assertTrue(self.policy.allows(self.store,batch['id']))
        other=self.store.prepare(self.inputs,{'revision':'different-bounded'},pilot=False)
        self.assertFalse(self.policy.allows(self.store,other['batch']))
        receipt['allowedBatch']['sourceHashes']=['a'*64];atomic(self.config['scopedBatchReadinessReceipt'],receipt)
        self.assertFalse(self.policy.allows(self.store,batch['id']))

    def test_bounded_batch_rejects_changed_issue_proof_budget_or_authorization(self):
        batch,receipt=self.scoped_fixture()
        changed=self.store.batch(batch['id']);changed['perPaperCap']+=1
        with self.store.transaction():self.store.save_batch(changed)
        self.assertFalse(self.policy.allows(self.store,batch['id']))
        changed['perPaperCap']-=1
        with self.store.transaction():self.store.save_batch(changed)
        self.assertTrue(self.policy.allows(self.store,batch['id']))
        Path(receipt['authorization']['receipt']).write_text('{}')
        self.assertFalse(self.policy.enabled())

    def test_bounded_batch_expiry_pauses_without_resetting_spend(self):
        batch,receipt=self.scoped_fixture();self.store.start(batch['id'],batch['summaryHash']);before=self.store.budgets.status(batch['items'][0]['job'])
        receipt['expiresAt']=time.time()-1;atomic(self.config['scopedBatchReadinessReceipt'],receipt)
        self.policy.guard(self.store)
        self.assertEqual(self.store.batch(batch['id'])['state'],'paused')
        self.assertEqual(self.store.budgets.status(batch['items'][0]['job']),before)

