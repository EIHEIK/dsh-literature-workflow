import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic,digest
from stage_handlers import TranslateHandler
import test_batches


class LegacyTranslationTests(unittest.TestCase):
    tearDown=test_batches.BatchTests.tearDown
    def setUp(self):
        test_batches.BatchTests.setUp(self)
        config={'revision':'import-fixture','importTranslation':{'path':str(self.source),'sha256':digest(self.source),'mapping':[1,2]}}
        summary=self.store.prepare([{'source':str(self.source)}],config)
        self.job=self.store.get(summary['items'][0]['job']);self.phase=self.store.phase(self.job['id'])
        self.folder=self.store.folder(self.job)/'import-worker';self.folder.mkdir()
        import shutil
        shutil.copyfile(self.source,self.folder/'mono.pdf');atomic(self.folder/'mapping.json',{'sourcePages':[1,2]})

    def test_import_has_no_model_policy_or_fabricated_provider_receipt(self):
        handler=TranslateHandler();prepared=handler.prepare(self.store,self.phase,self.job)
        self.assertEqual(prepared['settings']['mode'],'import_translation');self.assertNotIn('policy',prepared)
        result=handler.finish(self.store,self.phase,{'work':str(self.folder)}, {})
        receipt=json.loads(Path(result['artifacts']['importProvenance']['path']).read_text())
        self.assertFalse(receipt['oldAcceptanceInherited']);self.assertEqual(receipt['status'],'imported_unreviewed')
        self.assertNotIn('translationCalls',result['artifacts'])

    def test_changed_import_bytes_and_mapping_are_rejected(self):
        handler=TranslateHandler();atomic(self.folder/'mapping.json',{'sourcePages':[1,1]})
        with self.assertRaisesRegex(ValueError,'mapping changed'):handler.finish(self.store,self.phase,{'work':str(self.folder)}, {})
        (self.folder/'mono.pdf').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'bytes changed'):handler.finish(self.store,self.phase,{'work':str(self.folder)}, {})
