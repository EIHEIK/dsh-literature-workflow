"""Durable, transactional cost reservations shared by every attempt of a job.

Money uses integer micro-yuan (1 CNY = 1,000,000 units). Unknown provider usage
keeps its reservation; a process crash is never grounds to refund it.
"""
from contextlib import contextmanager
import time
import uuid

CNY = 1_000_000


class BudgetExceeded(ValueError):
    pass


def amount(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 10**12:
        raise ValueError('Cost must be a bounded integer in micro-yuan')
    return value


class BudgetLedger:
    def __init__(self, db):
        self.db = db
        if db.in_transaction: raise ValueError('Initialize ledger outside a transaction')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS budget_scopes(
                id TEXT PRIMARY KEY, cap INTEGER NOT NULL, target INTEGER NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS budget_requests(
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                reserved INTEGER NOT NULL, actual INTEGER,
                state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS budget_allocations(
                request_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                PRIMARY KEY(request_id,scope_id));
            CREATE INDEX IF NOT EXISTS budget_scope_allocations ON budget_allocations(scope_id);
        ''')

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        savepoint = 'budget_' + uuid.uuid4().hex
        self.db.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
        try:
            yield
            if nested: self.db.execute('RELEASE SAVEPOINT ' + savepoint)
            else: self.db.commit()
        except BaseException:
            if nested:
                self.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                self.db.execute('RELEASE SAVEPOINT ' + savepoint)
            else: self.db.rollback()
            raise

    def create_scope(self, scope, cap, target=None):
        amount(cap)
        target = cap if target is None else amount(target)
        if not isinstance(scope, str) or not scope or target > cap:
            raise ValueError('Invalid budget scope')
        with self.transaction():
            row = self.db.execute('SELECT cap,target FROM budget_scopes WHERE id=?', (scope,)).fetchone()
            if row and tuple(row) != (cap, target):
                raise ValueError('An existing budget cannot be silently changed')
            self.db.execute('INSERT OR IGNORE INTO budget_scopes(id,cap,target) VALUES(?,?,?)', (scope, cap, target))
        return self.status(scope)

    def status(self, scope):
        row = self.db.execute('SELECT cap,target,paused FROM budget_scopes WHERE id=?', (scope,)).fetchone()
        if row is None: raise ValueError('Unknown budget scope')
        cap, target, paused = row
        rows = self.db.execute('''SELECT r.state,r.reserved,r.actual FROM budget_requests r
                                 JOIN budget_allocations a ON a.request_id=r.id WHERE a.scope_id=?''', (scope,))
        spent = held = unknown = 0
        for state, reserved, actual in rows:
            if state == 'completed': spent += actual
            elif state in ('reserved', 'dispatched'): held += reserved
            elif state == 'unknown': unknown += reserved
        committed = spent + held + unknown
        return {'scope': scope, 'cap': cap, 'target': target, 'spent': spent,
                'reserved': held, 'unknown': unknown, 'available': max(0, cap - committed),
                'targetReached': committed >= target, 'paused': bool(paused),
                'overrun': max(0, committed - cap), 'unit': 'micro-CNY'}

    def request(self, request_id):
        row = self.db.execute('SELECT fingerprint,reserved,actual,state FROM budget_requests WHERE id=?', (request_id,)).fetchone()
        if row is None: raise ValueError('Unknown request')
        scopes = [r[0] for r in self.db.execute('SELECT scope_id FROM budget_allocations WHERE request_id=? ORDER BY scope_id', (request_id,))]
        return dict(id=request_id, fingerprint=row[0], reserved=row[1], actual=row[2], state=row[3], scopes=scopes)

    def reserve(self, request_id, scopes, worst_case, fingerprint):
        amount(worst_case)
        if not request_id or not fingerprint or not scopes or any(not isinstance(s, str) or not s for s in scopes):
            raise ValueError('Request identity, fingerprint and scopes are required')
        scopes = sorted(set(scopes))
        with self.transaction():
            exists = self.db.execute('SELECT 1 FROM budget_requests WHERE id=?', (request_id,)).fetchone()
            if exists:
                old = self.request(request_id)
                if old['fingerprint'] != fingerprint or old['reserved'] != worst_case or old['scopes'] != scopes:
                    raise ValueError('Request id reused with different cost, input or scope')
                return old
            for scope in scopes:
                current = self.status(scope)
                if current['paused'] or current['available'] < worst_case:
                    raise BudgetExceeded('Budget cannot cover the full request: ' + scope)
            self.db.execute('INSERT INTO budget_requests VALUES(?,?,?,?,?,?)',
                            (request_id, fingerprint, worst_case, None, 'reserved', time.time()))
            self.db.executemany('INSERT INTO budget_allocations VALUES(?,?)', [(request_id, s) for s in scopes])
        return self.request(request_id)

    def mark_dispatched(self, request_id):
        """Persist BEFORE sending bytes, so a crash cannot make a sent call refundable."""
        with self.transaction():
            record = self.request(request_id)
            if record['state'] != 'reserved': raise ValueError('Request already dispatched or settled; do not resend')
            self.db.execute("UPDATE budget_requests SET state='dispatched',updated=? WHERE id=?", (time.time(), request_id))
        return self.request(request_id)

    def mark_unknown(self, request_id):
        with self.transaction():
            record = self.request(request_id)
            if record['state'] not in ('dispatched', 'unknown'): raise ValueError('Only dispatched usage can become unknown')
            self.db.execute("UPDATE budget_requests SET state='unknown',updated=? WHERE id=?", (time.time(), request_id))
        return self.request(request_id)

    def release_unsent(self, request_id):
        """Only the adapter BEFORE dispatch may invoke this; timeouts are unknown."""
        with self.transaction():
            record = self.request(request_id)
            if record['state'] == 'released': return record
            if record['state'] != 'reserved': raise ValueError('Cannot refund a sent or settled request')
            self.db.execute("UPDATE budget_requests SET state='released',updated=? WHERE id=?", (time.time(), request_id))
        return self.request(request_id)

    def settle(self, request_id, actual):
        amount(actual, zero=True)
        with self.transaction():
            record = self.request(request_id)
            if record['state'] == 'completed':
                if record['actual'] != actual: raise ValueError('Conflicting usage for settled request')
                return record
            if record['state'] not in ('dispatched', 'unknown'): raise ValueError('Only dispatched requests can be settled')
            self.db.execute("UPDATE budget_requests SET actual=?,state='completed',updated=? WHERE id=?",
                            (actual, time.time(), request_id))
            # Preserve observed charges even if an upstream response violates the
            # reservation estimate. Stop future dispatch; never roll actual cost back.
            for scope in record['scopes']:
                if actual > record['reserved'] or self.status(scope)['overrun']:
                    self.db.execute('UPDATE budget_scopes SET paused=1 WHERE id=?', (scope,))
        return self.request(request_id)
