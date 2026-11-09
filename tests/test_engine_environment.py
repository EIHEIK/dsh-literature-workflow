import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from engine_environment import engine_environment


class EngineEnvironmentTests(unittest.TestCase):
    def test_real_child_cannot_inherit_keys_and_writes_temp_in_job(self):
        base = Path(__file__).resolve().parents[1] / '.test-output'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temp:
            before = dict(os.environ)
            parent = {**before, 'DEEPSEEK_API_KEY': 'secret', 'NEW_PROVIDER_TOKEN': 'other',
                      'PYTHONPATH': '/untrusted', 'HOME': '/outside'}
            env = engine_environment(temp, parent)
            self.assertEqual(before, dict(os.environ))
            result = subprocess.run([sys.executable, '-c',
                "import os,tempfile; assert 'DEEPSEEK_API_KEY' not in os.environ; "
                "assert 'NEW_PROVIDER_TOKEN' not in os.environ; assert 'PYTHONPATH' not in os.environ; "
                "f=tempfile.NamedTemporaryFile(delete=False); print(f.name); f.close()"],
                env=env, capture_output=True, text=True, encoding='utf-8', check=True)
            child_file = Path(result.stdout.strip()).resolve()
            self.assertTrue(child_file.is_relative_to(Path(temp).resolve()))
            self.assertTrue(child_file.exists())

    def test_each_job_has_distinct_writable_directories(self):
        base = Path(__file__).resolve().parents[1] / '.test-output'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temp:
            first = engine_environment(Path(temp) / 'one')
            second = engine_environment(Path(temp) / 'two')
            self.assertNotEqual(first['HOME'], second['HOME'])
            self.assertNotEqual(first['TEMP'], second['TEMP'])


if __name__ == '__main__': unittest.main()
