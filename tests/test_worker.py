import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import Store
import worker
from test_workflow import pdf

class OfflineWorkerTests(unittest.TestCase):
    def test_engine_version_survives_rich_warning_noise(self):
        noisy=('WARNING:pdf2zh_next.config.cli_env_model:No translation engine selected\n'*8)+'pdf2zh-next version: 2.10.2\n'
        self.assertEqual(worker.parse_engine_version(noisy),'2.10.2')
        self.assertIn('2.10.2',worker.parse_engine_version(noisy))
        self.assertEqual(worker.parse_engine_version('pdf2zh-next version: 2.11.0'),'2.11.0')

    def test_real_subprocess_engine_fixture_stops_at_needs_review(self):
        base=Path(__file__).resolve().parents[1]/'.test-output';base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            root=Path(folder);source=root/'source.pdf';pdf(source)
            s=Store(root/'state')
            with s.transaction():
                j=s.create({'source':str(source),'config':{'engine':sys.executable,'timeoutSeconds':60}},'synthetic-producer');j=s.claim(j['id'],'synthetic-producer')
            fake=root/'fake_engine.py'
            fake.write_text("import sys,pathlib,shutil\na=sys.argv;out=pathlib.Path(a[a.index('--output')+1]);shutil.copyfile(a[1],out/'fixture.mono.pdf')\n",encoding='utf-8')
            original=worker.subprocess.Popen
            def popen(argv,**kwargs):return original([sys.executable,str(fake),*argv[1:]],**kwargs)
            class OfflineMeter:
                token='synthetic-loopback-token';stopped=False
                def __init__(self,*args,**kwargs):pass
                def start(self):return 'http://127.0.0.1:1/v1'
                def close(self):pass
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'SYNTHETIC-TEST-ONLY'}),patch.object(worker,'probe',return_value={'version':'synthetic-engine','help':' '.join(worker.REQUIRED)}),patch.object(worker,'Meter',OfflineMeter),patch.object(worker.subprocess,'Popen',side_effect=popen):
                worker.run(str(s.root),j['id'],j['lease'])
            result=s.get(j['id']);s.db.close()
            self.assertEqual(result['state'],'needs_review',result.get('error'));self.assertIsNone(result['acceptance'])
            self.assertTrue(Path(result['candidate']['files']['cn']['path']).exists())

if __name__=='__main__':unittest.main()
