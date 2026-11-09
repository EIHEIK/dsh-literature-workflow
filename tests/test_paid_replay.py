from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'runtime'),str(ROOT/'scripts')]
from core import digest,object_hash
from repair_ops import POLICY
from replay_paid_repairs import archived_system


class ArchivedPromptTests(unittest.TestCase):
    def test_prompt_augmentation_is_reconstructed_without_executing_archived_code(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-output') as temporary:
            root=Path(temporary);path=root/'repair_ops.py'
            path.write_text('POLICY='+repr(POLICY)+'\nSYSTEM="base rule"\nSYSTEM+="; final rule"\nraise RuntimeError("must not execute")\n')
            sha=object_hash({path.name:digest(path)})
            self.assertEqual(archived_system(root,sha),'base rule; final rule')
            path.write_text(path.read_text()+'# changed\n')
            with self.assertRaisesRegex(ValueError,'runtime changed'):archived_system(root,sha)
