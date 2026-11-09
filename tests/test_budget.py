import concurrent.futures
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from budget import BudgetLedger, BudgetExceeded, CNY


class BudgetTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-output'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.path = Path(self.temp.name) / 'budget.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=20)
        self.ledger = BudgetLedger(self.db)
        self.ledger.create_scope('paper', 10*CNY, 5*CNY)
        self.ledger.create_scope('pilot', 15*CNY)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_unknown_cost_survives_reopen_and_cannot_be_refunded(self):
        self.ledger.reserve('one', ['paper','pilot'], 6*CNY, 'input1')
        self.ledger.mark_dispatched('one')
        self.ledger.mark_unknown('one')
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.ledger = BudgetLedger(self.db)
        self.assertEqual(self.ledger.status('paper')['unknown'], 6*CNY)
        with self.assertRaises(BudgetExceeded): self.ledger.reserve('two', ['paper'], 5*CNY, 'input2')
        with self.assertRaises(ValueError): self.ledger.release_unsent('one')

    def test_repeated_request_is_idempotent_and_scope_cannot_change(self):
        a = self.ledger.reserve('one', ['paper','pilot'], 6*CNY, 'input1')
        self.assertEqual(a, self.ledger.reserve('one', ['pilot','paper'], 6*CNY, 'input1'))
        self.assertEqual(self.ledger.status('paper')['reserved'], 6*CNY)
        with self.assertRaises(ValueError): self.ledger.reserve('one', ['paper'], 6*CNY, 'input1')
        with self.assertRaises(ValueError): self.ledger.create_scope('paper', 20*CNY)

    def test_overrun_is_recorded_and_stops_new_calls(self):
        self.ledger.reserve('one', ['paper'], CNY, 'input1')
        self.ledger.mark_dispatched('one')
        self.ledger.settle('one', 2*CNY)
        self.assertEqual(self.ledger.status('paper')['spent'], 2*CNY)
        self.assertTrue(self.ledger.status('paper')['paused'])
        with self.assertRaises(BudgetExceeded): self.ledger.reserve('two', ['paper'], CNY, 'input2')

    def test_settlement_and_pilot_cap_span_multiple_papers(self):
        self.ledger.create_scope('paper2', 10*CNY)
        self.ledger.reserve('one', ['paper','pilot'], 10*CNY, 'input1')
        self.ledger.mark_dispatched('one')
        self.ledger.settle('one', 8*CNY)
        with self.assertRaises(BudgetExceeded): self.ledger.reserve('two', ['paper2','pilot'], 8*CNY, 'input2')
        self.ledger.reserve('two', ['paper2','pilot'], 7*CNY, 'input2')
        self.ledger.mark_dispatched('two')
        self.ledger.settle('two', 6*CNY)
        self.assertEqual(self.ledger.status('pilot')['spent'], 14*CNY)

    def test_concurrent_reservations_cannot_overbook(self):
        def claim(n):
            db = sqlite3.connect(self.path, timeout=20)
            try:
                ledger = BudgetLedger(db)
                ledger.reserve('request'+str(n), ['paper'], 6*CNY, str(n))
                return True
            except BudgetExceeded: return False
            finally: db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [1,2]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.ledger.status('paper')['reserved'], 6*CNY)

    def test_dispatched_request_cannot_be_refunded_or_dispatched_twice(self):
        self.ledger.reserve('one', ['paper'], CNY, 'input1')
        self.ledger.mark_dispatched('one')
        with self.assertRaises(ValueError): self.ledger.release_unsent('one')
        with self.assertRaises(ValueError): self.ledger.mark_dispatched('one')
        self.assertEqual(self.ledger.status('paper')['reserved'], CNY)


if __name__ == '__main__': unittest.main()
