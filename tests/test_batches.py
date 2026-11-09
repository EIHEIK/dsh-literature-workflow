import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from batches import BatchStore
from core import digest
from test_workflow import pdf


class BatchTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-output'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.pdf'
        pdf(self.source)
        self.store = BatchStore(self.root / 'data')
        self.inputs = [{'source': str(self.source), 'title': 'Fixture'}]
        self.summary = self.store.prepare(self.inputs, {'revision': 'test'})
        self.batch = self.summary['batch']
        self.job = self.summary['items'][0]['job']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def start(self):
        self.store.confirm(self.batch, self.summary['summaryHash'], 'synthetic-user-confirmation')
        return self.store.start(self.batch, self.summary['summaryHash'])

    def artifact(self, name='background.json'):
        path = self.store.folder(self.store.get(self.job)) / name
        path.write_text('{}', encoding='utf-8')
        return {'data': {'path': str(path), 'sha256': digest(path)}}

    def test_no_work_before_confirmation_and_duplicate_start_is_idempotent(self):
        self.assertIsNone(self.store.claim_next())
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.store.start(self.batch, self.summary['summaryHash'])
        self.start()
        first = self.store.claim_next()
        self.store.start(self.batch, self.summary['summaryHash'])
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(first['token'], self.store.phase(self.job)['token'])

    def test_revision_keeps_historical_spending_in_shared_paper_cap(self):
        ledger=self.store.budgets
        ledger.reserve('old-call',[self.job,'pilot:first'],100000,'old-fixture')
        ledger.mark_dispatched('old-call');ledger.settle('old-call',94188)
        revised=self.store.prepare(self.inputs,{'revision':'new-code-fixture'})
        new_job=revised['items'][0]['job'];scope=self.store.get(new_job)['budgetScope']
        self.assertEqual(ledger.status(scope)['spent'],94188)
        self.assertEqual(ledger.status('pilot:first')['spent'],94188)
        self.store.prepare(self.inputs,{'revision':'new-code-fixture'})
        self.assertEqual(ledger.status(scope)['spent'],94188)
        self.assertEqual(ledger.status(scope)['available'],10_000_000-94188)

    def test_input_tampering_invalidates_confirmation(self):
        Path(self.store.get(self.job)['source']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.store.confirm(self.batch, self.summary['summaryHash'], 'synthetic-confirmation')

    def test_prepare_dedup_and_nested_budget_transaction_rollback(self):
        self.assertEqual(self.store.prepare(self.inputs, {'revision': 'test'})['batch'], self.batch)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store.transaction():
                self.store.budgets.create_scope('transient', 100)
                raise RuntimeError('rollback')
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.store.budgets.status('transient')

    def test_pause_finishes_inflight_checkpoint_but_does_not_dispatch_next(self):
        self.start(); lease = self.store.claim_next()
        self.store.control(self.batch, 'pause')
        self.store.finish(self.job, lease['token'], self.artifact())
        self.assertIsNone(self.store.claim_next())
        self.store.control(self.batch, 'resume')
        self.assertEqual(self.store.claim_next()['stage'], 'translate')

    def test_cancel_rejects_late_result(self):
        self.start(); lease = self.store.claim_next()
        self.store.control(self.batch, 'cancel')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.finish(self.job, lease['token'], self.artifact())

    def test_expired_lease_does_not_authorize_duplicate_worker(self):
        self.start(); lease = self.store.claim_next()
        with self.store.transaction():
            current = self.store.phase(self.job); current['leaseUntil'] = 1
            self.store.save_phase(current)
        self.assertIsNone(self.store.claim_next())
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.store.recover(self.job, lease['token'], {'state': 'observation_timeout'})
        self.store.recover(self.job, lease['token'], {'state': 'missing', 'checkedAt': time.time()})
        next_lease = self.store.claim_next()
        self.assertNotEqual(next_lease['token'], lease['token'])

    def test_restart_retains_checkpoint_and_events_cursor(self):
        self.start(); lease = self.store.claim_next()
        self.store.finish(self.job, lease['token'], self.artifact())
        events = self.store.read_events()
        self.store.db.close(); self.store = BatchStore(self.root / 'data')
        self.assertEqual(self.store.claim_next()['stage'], 'translate')
        new = self.store.read_events(events['cursor'])
        self.assertEqual(len(new['events']), 1)
        self.assertEqual(new['events'][0]['kind'], 'stage.started')
        self.assertNotIn('token', json.dumps(self.store.status(self.batch)))

    def test_cross_connection_claims_have_one_winner(self):
        self.start()
        def claim(_):
            other = BatchStore(self.root / 'data')
            try: return other.claim_next()
            finally: other.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_failed_paper_does_not_block_other_paper(self):
        source2 = self.root / 'other.pdf'; pdf(source2, label='different')
        other = self.store.prepare([{'source': str(source2)}], {'revision': 'test'})
        self.store.confirm(other['batch'], other['summaryHash'], 'synthetic-confirmation')
        self.store.start(other['batch'], other['summaryHash'])
        self.start(); lease = self.store.claim_next()
        self.store.fail(lease['job'], lease['token'], 'parse', 'Cannot read equation')
        next_lease = self.store.claim_next()
        self.assertIsNotNone(next_lease)
        self.assertNotEqual(lease['job'], next_lease['job'])

    def test_empty_evidence_and_unregistered_review_cannot_advance(self):
        self.start(); lease = self.store.claim_next()
        with self.assertRaisesRegex(ValueError, 'artifact evidence'):
            self.store.finish(self.job, lease['token'], {})
        with self.store.transaction():
            phase = self.store.phase(self.job); phase['stage'] = 'review'; self.store.save_phase(phase)
        with self.assertRaisesRegex(ValueError, 'registered'):
            self.store.finish(self.job, lease['token'], self.artifact(), verdict='pass')

    def test_changed_earlier_checkpoint_blocks_reuse_after_later_stage(self):
        self.start(); lease = self.store.claim_next()
        first = self.artifact('background.json')
        self.store.finish(self.job, lease['token'], first)
        lease = self.store.claim_next()
        self.store.finish(self.job, lease['token'], self.artifact('translation.json'))
        Path(first['data']['path']).write_text('{"changed":true}', encoding='utf-8')
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.phase(self.job)['error']['kind'], 'integrity')
        self.assertEqual(self.store.status(self.batch)['state'], 'needs_attention')

    def test_runtime_change_cannot_reuse_checkpoint_or_confirm_old_summary(self):
        with patch('batches.code_hash', return_value='new-runtime'):
            with self.assertRaisesRegex(ValueError, 'Runtime changed'):
                self.start()
        self.start(); lease = self.store.claim_next()
        self.store.finish(self.job, lease['token'], self.artifact())
        with patch('batches.code_hash', return_value='new-runtime'):
            self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.phase(self.job)['error']['kind'], 'integrity')

    def test_checkpoint_body_tamper_cannot_be_reused(self):
        self.start(); lease = self.store.claim_next()
        self.store.finish(self.job, lease['token'], self.artifact())
        checkpoint_id = self.store.phase(self.job)['lastCheckpoint']
        with self.store.transaction():
            body = json.loads(self.store.db.execute('SELECT body FROM phase_checkpoints WHERE id=?', (checkpoint_id,)).fetchone()[0])
            body['artifacts'] = {}
            self.store.db.execute('UPDATE phase_checkpoints SET body=? WHERE id=?', (json.dumps(body), checkpoint_id))
        self.assertIsNone(self.store.claim_next())
        self.assertIn('identity mismatch', self.store.phase(self.job)['error']['message'])

    def test_shared_unfinished_job_cannot_make_another_batch_complete(self):
        other = self.store.prepare(self.inputs, {'revision': 'test'}, pilot=False)
        self.assertNotEqual(other['batch'], self.batch)
        self.assertEqual(other['items'][0]['existingBatch'], self.batch)
        self.store.confirm(other['batch'], other['summaryHash'], 'fixture-user')
        result = self.store.start(other['batch'], other['summaryHash'])
        self.assertNotEqual(result['state'], 'complete')
        self.assertIsNone(self.store.claim_next())  # Original owner still unconfirmed.


if __name__ == '__main__': unittest.main()
