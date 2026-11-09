import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from model_gate import ModelGate

SID = '01234567-1234-1234-1234-0123456789ab'


class ModelGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.test-output')
        self.root = Path(self.tmp.name); self.path = self.root/('rollout-fixture-'+SID+'.jsonl')
        self.write()
        self.gate = ModelGate(self.root)
    def tearDown(self): self.tmp.cleanup()
    def write(self, model='gpt-6.1-sol', effort='medium', identity=SID):
        self.path.write_text('\n'.join(json.dumps(v) for v in [
            {'type':'session_meta','payload':{'id':identity}},
            {'type':'turn_context','payload':{'model':model,'effort':effort,'turn_id':'fixture-turn'}}])+'\n', encoding='utf-8')
    def test_only_actual_matching_model_and_effort_allowed(self):
        self.assertEqual(self.gate.check(SID)['model'], 'gpt-6.1-sol')
        for model, effort in [('gpt-6-astra','medium'),('gpt-6.1-sol','high'),('gpt-6.1-sol',None)]:
            self.write(model, effort)
            with self.assertRaises(PermissionError): self.gate.check(SID)
    def test_missing_or_path_like_thread_ids_rejected(self):
        for value in (None, '', '../../private', {'model':'gpt-6.1-sol'}):
            with self.assertRaises(PermissionError): self.gate.check(value)
    def test_codex_records_user_selected_effort_without_relaxing_model(self):
        gate=ModelGate(self.root,expected_effort=None)
        self.write(effort='high');self.assertEqual(gate.check(SID)['effort'],'high')
        self.write(model='gpt-6-astra',effort='high')
        with self.assertRaises(PermissionError):gate.check(SID)
        self.write(effort=None)
        with self.assertRaises(PermissionError):gate.check(SID)
    def test_latest_turn_controls_gate_not_initial_config(self):
        with self.path.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'type':'turn_context','payload':{'model':'gpt-6-astra','effort':'medium','turn_id':'next'}})+'\n')
        with self.assertRaises(PermissionError): self.gate.check(SID)
    def test_stale_or_mismatched_session_rejected(self):
        os.utime(self.path, (1,1))
        with self.assertRaisesRegex(PermissionError, 'stale'): self.gate.check(SID)
        self.write(identity='another-thread')
        with self.assertRaisesRegex(PermissionError, 'mismatched'): self.gate.check(SID)
    def test_partial_new_turn_never_reuses_previous_allowed_model(self):
        with self.path.open('ab') as out: out.write(b'{"type":"turn_context","payload":')
        with self.assertRaisesRegex(PermissionError, 'incomplete'): self.gate.check(SID)


if __name__ == '__main__': unittest.main()
