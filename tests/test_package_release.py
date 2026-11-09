import json
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class PublicPackageTests(unittest.TestCase):
    def test_public_archive_keeps_code_and_excludes_private_operational_documents(self):
        result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/package_release.py')],cwd=ROOT,text=True,encoding='utf-8',capture_output=True,check=True)
        artifact=Path(json.loads(result.stdout)['package'])
        with tarfile.open(artifact,'r:gz') as package:
            names=package.getnames()
        self.assertIn('package/index.js',names);self.assertIn('package/docs/WORKFLOW.md',names)
        self.assertIn('package/runtime/container_worker.py',names)
        for name in ('IMPLEMENTATION_PLAN.md','IMPLEMENTATION_PLAN_CURRENT.md','IMPLEMENTATION_STATUS.md'):
            self.assertNotIn('package/docs/'+name,names)
        self.assertFalse(any('/.test-output/' in name or name.endswith(('.sqlite3','.env','.pyc')) for name in names))
