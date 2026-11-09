import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic,digest
from repaired_import import EXTRAS,validate_import


class RepairedImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output')
        self.root=Path(self.tmp.name);out=self.root/'output';out.mkdir()
        for name in ('mono.pdf',*EXTRAS.values()):(out/name).write_bytes(b'fixture artifact, not an actual paper')
        atomic(out/'mapping.json',{'sourcePages':[1,1,2]})
        files={p.name:digest(p) for p in out.iterdir()}
        package={'files':files,'sourceSha256':'a'*64,'sourcePages':[1,2],'repairRoundUsed':1,
                 'acceptance':'not_granted','fullIndependentReviewRequired':True}
        atomic(self.root/'package.json',package)
        self.imported={'path':str(out/'mono.pdf'),'sha256':files['mono.pdf'],'mapping':[1,1,2],'repairRoundUsed':1,
                       'repairPackage':{'path':str(self.root/'package.json'),'sha256':digest(self.root/'package.json')}}
        for label,name in EXTRAS.items():self.imported[label]={'path':str(out/name),'sha256':files[name]}

    def tearDown(self):self.tmp.cleanup()

    def test_round_is_carried_without_acceptance(self):
        self.assertEqual(validate_import(self.imported,'a'*64,[1,2]),1)

    def test_repaired_import_cannot_reset_round_or_change_source_scope(self):
        changed=copy.deepcopy(self.imported);changed['repairRoundUsed']=0
        with self.assertRaisesRegex(ValueError,'reset'):validate_import(changed,'a'*64,[1,2])
        with self.assertRaisesRegex(ValueError,'identity'):validate_import(self.imported,'b'*64,[1,2])

    def test_changed_mapping_or_artifact_is_rejected(self):
        changed=copy.deepcopy(self.imported);changed['mapping']=[1,2,1]
        with self.assertRaisesRegex(ValueError,'mapping'):validate_import(changed,'a'*64,[1,2])
        Path(self.imported['sourceQuotes']['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'artifact'):validate_import(self.imported,'a'*64,[1,2])
