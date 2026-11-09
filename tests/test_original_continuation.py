from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'runtime'),str(ROOT/'scripts')]
from core import digest
from continue_original_batch import verify_deferral


class DeferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output');self.path=Path(self.tmp.name)/'issue.zip';self.path.write_bytes(b'SYNTHETIC PROBLEM PACKAGE')
        self.job={'id':'paper-'+'a'*16,'sourceSha256':'b'*64,'sourcePages':[1,2],'acceptance':None,'publication':None}
        self.phase={'job':self.job['id'],'state':'blocked','stage':'review','repairRound':2,'error':{'kind':'quality'},
                    'problemPackage':{'path':str(self.path),'sha256':digest(self.path)}}
        self.cost={'spent':12,'reserved':0,'unknown':0,'paused':False}

    def tearDown(self):self.tmp.cleanup()

    def test_only_a_bounded_failed_paper_is_deferred_without_acceptance(self):
        result=verify_deferral(self.job,self.phase,self.cost)
        self.assertFalse(result['acceptanceGranted']);self.assertEqual(result['repairRound'],2)
        with self.assertRaisesRegex(ValueError,'same pilot'):verify_deferral(self.job,{**self.phase,'state':'running'},self.cost)
        with self.assertRaisesRegex(ValueError,'remaining bounded'):verify_deferral(self.job,{**self.phase,'repairRound':1},self.cost)

    def test_unknown_spend_accepted_paper_and_corrupt_bundle_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Unresolved'):verify_deferral(self.job,self.phase,{**self.cost,'unknown':1})
        with self.assertRaisesRegex(ValueError,'accepted pilot'):verify_deferral({**self.job,'acceptance':{'fixture':True}},self.phase,self.cost)
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'issue bundle'):verify_deferral(self.job,self.phase,self.cost)
