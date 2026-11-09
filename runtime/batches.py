"""Durable batch orchestration on the existing artifact/acceptance database.

Only the controller calls claim/finish/recover. Public clients receive summaries
and control operations, never worker lease tokens or arbitrary stage completion.
"""
import json
from pathlib import Path
import time
import uuid

from core import Store, canonical, object_hash, digest, code_hash
from budget import BudgetLedger, CNY

STAGES = ('background', 'translate', 'compose', 'review', 'accept', 'publish', 'archive')
TERMINAL = ('complete', 'cancelled')
LEASE_SECONDS = 300


class BatchStore(Store):
    def __init__(self, root):
        super().__init__(root)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batch_jobs(job_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,ordinal INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS phase_jobs(job_id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,job_id TEXT,kind TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS phase_checkpoints(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                stage TEXT NOT NULL,generation INTEGER NOT NULL,body TEXT NOT NULL);
        ''')
        self.budgets = BudgetLedger(self.db)

    def event(self, kind, batch=None, job=None, **data):
        self.db.execute('INSERT INTO workflow_events(batch_id,job_id,kind,created,body) VALUES(?,?,?,?,?)',
                        (batch, job, kind, time.time(), canonical(data)))

    def batch(self, batch_id):
        row = self.db.execute('SELECT body FROM batches WHERE id=?', (batch_id,)).fetchone()
        if row is None: raise ValueError('Unknown batch')
        return json.loads(row[0])

    def save_batch(self, batch):
        batch['updatedAt'] = time.time()
        self.db.execute('UPDATE batches SET body=? WHERE id=?', (canonical(batch), batch['id']))

    def phase(self, job_id):
        row = self.db.execute('SELECT body FROM phase_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None: raise ValueError('Unknown phase job')
        return json.loads(row[0])

    def save_phase(self, phase):
        self.db.execute('INSERT OR REPLACE INTO phase_jobs VALUES(?,?)', (phase['job'], canonical(phase)))

    def prepare(self, inputs, config, *, pilot=True, title='文献翻译批次'):
        """inputs are resolved by the trusted Zotero bridge, NOT public path arguments.

        Freeze a free local snapshot before the user reviews the batch. Paid work
        remains blocked until confirmation. All budgets live in this same database.
        """
        if not inputs: raise ValueError('No eligible source attachments')
        with self.transaction():
            jobs = [self.create({**item, 'config': config}, 'controller:producer',source_info=item.get('_sourceInfo')) for item in inputs]
            jobs = list({job['id']: job for job in jobs}.values())
            # Same source/range keeps one cap across explicit code revisions.
            # Link historical request allocations without refunding or charging
            # them again; each request is unique within the cumulative scope.
            for job in jobs:
                scope='paper-total:'+object_hash({'source':job['sourceSha256'],'pages':job['sourcePages']})[:32]
                self.budgets.create_scope(scope,10*CNY,5*CNY)
                siblings=[json.loads(row[0]) for row in self.db.execute('SELECT body FROM jobs')]
                for old in siblings:
                    if old['sourceSha256']==job['sourceSha256'] and old['sourcePages']==job['sourcePages']:
                        self.db.execute('INSERT OR IGNORE INTO budget_allocations(request_id,scope_id) SELECT request_id,? FROM budget_allocations WHERE scope_id=?',(scope,old['id']))
                job['budgetScope']=scope;self.save(job)
            frozen = [{'job': j['id'], 'sourceSha256': j['sourceSha256'], 'pages': j['sourcePages'],
                       'title': j['title'], 'zotero': j.get('zotero')} for j in jobs]
            fingerprint = object_hash({'inputs': frozen, 'config': config, 'pilot': pilot})
            existing = self.db.execute('SELECT id FROM batches WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing: return self.status(existing[0])
            batch_id = 'batch-' + uuid.uuid4().hex[:20]
            batch = {'id': batch_id, 'title': title, 'state': 'awaiting_confirmation',
                     'createdAt': time.time(), 'pilot': bool(pilot), 'items': frozen,
                     'configHash': object_hash(config), 'runtimeHash': code_hash(), 'perPaperTarget': 5*CNY,
                     'perPaperCap': 10*CNY, 'pilotCap': 15*CNY if pilot else None,
                     'batchCap': None, 'confirmation': None}
            for ordinal, job in enumerate(jobs):
                owner = self.db.execute('SELECT batch_id FROM batch_jobs WHERE job_id=?', (job['id'],)).fetchone()
                if owner:
                    frozen[ordinal]['existingBatch'] = owner[0]
                    continue
                self.db.execute('INSERT INTO batch_jobs VALUES(?,?,?)', (job['id'], batch_id, ordinal))
                from repaired_import import validate_import
                rounds=validate_import(config.get('importTranslation',{}),job['sourceSha256'],job['sourcePages'])
                phase = {'job': job['id'], 'batch': batch_id, 'stage': 'background', 'state': 'queued',
                         'generation': 0, 'token': None, 'leaseUntil': 0,
                         'recoveries': {}, 'repairRound': rounds, 'error': None, 'worker': None}
                self.save_phase(phase)
                self.budgets.create_scope(job['id'], 10*CNY, 5*CNY)
            if pilot: self.budgets.create_scope('pilot:first', 15*CNY)
            batch['summaryHash'] = object_hash({k: batch[k] for k in
                ('items', 'configHash', 'runtimeHash', 'pilot', 'perPaperTarget', 'perPaperCap', 'pilotCap', 'batchCap')})
            self.db.execute('INSERT INTO batches VALUES(?,?,?)', (batch_id, fingerprint, canonical(batch)))
            self.event('batch.prepared', batch_id, summaryHash=batch['summaryHash'], papers=len(frozen))
        return self.status(batch_id)

    def confirm(self, batch_id, summary_hash, receipt):
        """Trusted confirmation adapter only; not exposed as an agent MCP tool.

        receipt is supplied by the user-confirmation surface, not PDF/model text.
        """
        if not receipt or not isinstance(receipt, str): raise ValueError('User confirmation receipt required')
        with self.transaction():
            batch = self.batch(batch_id)
            if batch['summaryHash'] != summary_hash: raise ValueError('Batch summary changed; confirm the current summary')
            if batch['confirmation']:
                return self.status(batch_id)
            if batch['state'] != 'awaiting_confirmation': raise ValueError('Batch cannot be confirmed in current state')
            if batch.get('runtimeHash') != code_hash(): raise ValueError('Runtime changed; prepare a new batch revision')
            for item in batch['items']:
                job = self.get(item['job'])
                if digest(job['source']) != item['sourceSha256'] or object_hash(job['config']) != batch['configHash']:
                    raise ValueError('Frozen input or configuration changed')
            batch['confirmation'] = {'receipt': receipt, 'summaryHash': summary_hash, 'at': time.time()}
            batch['state'] = 'ready'
            self.save_batch(batch)
            self.event('batch.confirmed', batch_id, summaryHash=summary_hash)
        return self.status(batch_id)

    def start(self, batch_id, summary_hash):
        with self.transaction():
            batch = self.batch(batch_id)
            if batch['summaryHash'] != summary_hash or not batch['confirmation']:
                raise ValueError('The current batch summary requires user confirmation')
            if batch['state'] in ('running', 'complete'): return self.status(batch_id)
            if batch['state'] != 'ready': raise ValueError('Use resume for a paused batch')
            batch['state'] = 'running'
            self.save_batch(batch)
            self.event('batch.started', batch_id)
            self.refresh_batch(batch_id)
        return self.status(batch_id)

    def control(self, batch_id, action):
        if action not in ('pause', 'resume', 'cancel'): raise ValueError('Unsupported batch control')
        with self.transaction():
            batch = self.batch(batch_id)
            if batch['state'] in TERMINAL:
                if action == 'cancel' and batch['state'] == 'cancelled': return self.status(batch_id)
                raise ValueError('Terminal batch cannot be changed')
            if not batch['confirmation']: raise ValueError('Batch requires confirmation before control')
            if action == 'resume' and batch['state'] not in ('paused', 'running'):
                raise ValueError('Resolve blocked jobs explicitly; resume does not reset failures or budgets')
            batch['state'] = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[action]
            self.save_batch(batch)
            if action == 'cancel':
                for job_id, in self.db.execute('SELECT job_id FROM batch_jobs WHERE batch_id=?', (batch_id,)).fetchall():
                    phase = self.phase(job_id)
                    if phase['state'] == 'done': continue
                    phase.update(state='cancelled', token=None, leaseUntil=0, generation=phase['generation']+1)
                    self.save_phase(phase)
            self.event('batch.' + action, batch_id)
        return self.status(batch_id)

    def claim_next(self):
        with self.transaction():
            phases = [json.loads(r[0]) for r in self.db.execute('SELECT body FROM phase_jobs')]
            # An expired lease is not proof the worker stopped. Recovery requires
            # actual terminal/missing-worker evidence before another claim.
            if any(p['state'] == 'running' for p in phases): return None
            for phase in phases:
                if phase['state'] != 'queued' or self.batch(phase['batch'])['state'] != 'running': continue
                try:
                    self.verify_checkpoints(phase)
                except (ValueError, OSError, KeyError) as error:
                    phase.update(state='blocked', token=None, leaseUntil=0,
                                 error={'kind': 'integrity', 'message': str(error)[:1500]})
                    self.save_phase(phase)
                    self.event('stage.blocked', phase['batch'], phase['job'],
                               stage=phase['stage'], kindOfFailure='integrity')
                    self.refresh_batch(phase['batch'])
                    continue
                phase.update(state='running', token=uuid.uuid4().hex, leaseUntil=time.time()+LEASE_SECONDS,
                             generation=phase['generation']+1, heartbeatAt=time.time(), progressAt=time.time(), worker=None)
                self.save_phase(phase)
                self.event('stage.started', phase['batch'], phase['job'], stage=phase['stage'], generation=phase['generation'])
                return phase
        return None

    def verify_checkpoints(self, phase):
        """Validate the full dependency chain before reuse, including old stages.

        A changed input, runtime, or artifact blocks this paper instead of silently
        treating a successful previous run as still valid. Updates need an explicit
        new revision; recovery is not an implicit migration mechanism.
        """
        job = self.get(phase['job']); batch = self.batch(phase['batch'])
        source = Path(job['source']).resolve(strict=True)
        root = self.folder(job).resolve(strict=True)
        if not source.is_relative_to(root) or digest(source) != job['sourceSha256']:
            raise ValueError('Frozen source changed or escaped its job')
        if object_hash(job['config']) != batch['configHash']:
            raise ValueError('Frozen configuration changed')
        if batch.get('runtimeHash') != code_hash():
            raise ValueError('Runtime changed; explicit batch revision required')
        current = phase.get('lastCheckpoint'); visited = set(); newer_generation = phase['generation'] + 1
        while current:
            if current in visited: raise ValueError('Checkpoint dependency cycle')
            visited.add(current)
            row = self.db.execute('SELECT job_id,stage,generation,body FROM phase_checkpoints WHERE id=?', (current,)).fetchone()
            if row is None: raise ValueError('Missing checkpoint dependency')
            checkpoint = json.loads(row[3])
            if object_hash(checkpoint) != current or row[0] != job['id'] or checkpoint['job'] != job['id']:
                raise ValueError('Checkpoint identity mismatch')
            if row[1] != checkpoint['stage'] or row[2] != checkpoint['generation'] or not 0 < row[2] < newer_generation:
                raise ValueError('Checkpoint sequence mismatch')
            newer_generation = row[2]
            if (checkpoint['sourceSha256'] != job['sourceSha256'] or
                    checkpoint['configHash'] != batch['configHash'] or
                    checkpoint.get('runtimeHash') != batch['runtimeHash']):
                raise ValueError('Checkpoint input or runtime mismatch')
            for artifact in checkpoint['artifacts'].values():
                path = Path(artifact['path']).resolve(strict=True)
                if not path.is_relative_to(root) or digest(path) != artifact['sha256']:
                    raise ValueError('Checkpoint artifact changed or escaped its job')
            current = checkpoint['previous']

    def valid_lease(self, job_id, token):
        phase = self.phase(job_id)
        if phase['state'] != 'running' or phase['token'] != token or phase['leaseUntil'] < time.time():
            raise ValueError('Expired, cancelled or stale phase lease')
        return phase

    def heartbeat(self, job_id, token, *, progress=False, worker=None):
        with self.transaction():
            phase = self.valid_lease(job_id, token)
            phase.update(leaseUntil=time.time()+LEASE_SECONDS, heartbeatAt=time.time())
            if progress: phase['progressAt'] = time.time()
            if worker is not None:
                if phase['worker'] not in (None, worker): raise ValueError('Worker identity changed')
                phase['worker'] = worker
            self.save_phase(phase)

    def finish(self, job_id, token, artifacts, *, verdict=None):
        with self.transaction():
            phase = self.valid_lease(job_id, token)
            self.verify_checkpoints(phase)
            job = self.get(job_id)
            if digest(job['source']) != job['sourceSha256']: raise ValueError('Frozen source changed')
            if phase['stage'] in ('background', 'translate', 'compose', 'review', 'repair') and not artifacts:
                raise ValueError('Stage requires durable artifact evidence')
            if phase['stage'] == 'review':
                row = self.db.execute('SELECT body FROM tickets WHERE id=?', (job.get('lastReview'),)).fetchone()
                if row is None: raise ValueError('Independent review must be registered before stage completion')
                ticket = json.loads(row[0]); report = ticket.get('report')
                if not report or digest(report['path']) != report['sha256']:
                    raise ValueError('Review evidence missing or changed')
                with open(report['path'], encoding='utf-8') as stream: review = json.load(stream)
                if not job.get('candidate') or ticket['candidate'] != job['candidate']['id'] or review['verdict'] != verdict:
                    raise ValueError('Review stage does not match the current candidate and report')
            if phase['stage'] in ('accept', 'publish', 'archive'):
                if not job.get('acceptance') or job['acceptance'] != self.acceptance(job):
                    raise ValueError('Actual candidate-bound acceptance is required')
            if phase['stage'] in ('publish', 'archive'):
                if not job.get('publication'): raise ValueError('Actual publication is required')
                for file in job['publication']['outputs'].values():
                    if digest(file['path']) != file['sha256']: raise ValueError('Publication changed')
            if phase['stage'] == 'archive':
                if job['state'] != 'archived' or set(job.get('archive', {})) != {'cn', 'bi'}:
                    raise ValueError('Both Zotero receipts are required')
                for mode, receipt in job['archive'].items():
                    if receipt.get('state') != 'verified' or receipt.get('sha256') != job['publication']['outputs'][mode]['sha256']:
                        raise ValueError('Zotero receipt mismatch')
            checked = {}
            job_root = self.folder(job).resolve()
            for name, value in artifacts.items():
                path = Path(value['path']).resolve(strict=True)
                if not path.is_relative_to(job_root): raise ValueError('Checkpoint artifact outside its job')
                actual = digest(path)
                if actual != value['sha256']: raise ValueError('Checkpoint artifact hash mismatch')
                checked[name] = {'path': str(path), 'sha256': actual}
            checkpoint = {'job': job_id, 'stage': phase['stage'], 'generation': phase['generation'],
                          'artifacts': checked, 'at': time.time(), 'verdict': verdict,
                          'sourceSha256': job['sourceSha256'], 'configHash': object_hash(job['config']),
                          'runtimeHash': self.batch(phase['batch'])['runtimeHash'],
                          'previous': phase.get('lastCheckpoint')}
            checkpoint_id = object_hash(checkpoint)
            self.db.execute('INSERT INTO phase_checkpoints VALUES(?,?,?,?,?)',
                (checkpoint_id, job_id, phase['stage'], phase['generation'], canonical(checkpoint)))
            current_stage = phase['stage']
            if current_stage == 'review':
                if verdict not in ('pass', 'needs_attention'): raise ValueError('Review requires an explicit verdict')
                if verdict == 'needs_attention':
                    if phase['repairRound'] >= 2:
                        phase.update(state='blocked', error={'kind': 'review', 'message': 'Automatic repair limit reached'})
                    else:
                        phase.update(stage='repair', state='queued', repairRound=phase['repairRound']+1)
                else: phase.update(stage='accept', state='queued')
            elif current_stage == 'repair': phase.update(stage='compose', state='queued')
            elif current_stage == 'archive': phase.update(state='done')
            else: phase.update(stage=STAGES[STAGES.index(current_stage)+1], state='queued')
            phase.update(token=None, leaseUntil=0, worker=None, lastCheckpoint=checkpoint_id)
            self.save_phase(phase)
            self.event('stage.finished', phase['batch'], job_id, stage=current_stage, nextStage=phase['stage'], state=phase['state'])
            self.refresh_batch(phase['batch'])
        return self.status(phase['batch'])

    def fail(self, job_id, token, kind, message):
        with self.transaction():
            phase = self.valid_lease(job_id, token)
            phase.update(state='blocked', token=None, leaseUntil=0,
                         error={'kind': kind, 'message': str(message)[:1500]})
            self.save_phase(phase)
            self.event('stage.blocked', phase['batch'], job_id, stage=phase['stage'], kindOfFailure=kind,
                       message=str(message)[:1500])
            self.refresh_batch(phase['batch'])

    def recover(self, job_id, token, evidence):
        """Controller supplies verified Docker/process evidence; never public model input."""
        if evidence.get('state') not in ('terminal', 'missing') or not evidence.get('checkedAt'):
            raise ValueError('Actual terminal/missing worker evidence required; timeout is insufficient')
        with self.transaction():
            phase = self.phase(job_id)
            if phase['state'] != 'running' or phase['token'] != token: raise ValueError('Stale recovery token')
            if phase['worker'] is not None and evidence.get('worker') != phase['worker']:
                raise ValueError('Recovery evidence identifies another worker')
            if abs(time.time()-evidence['checkedAt']) > 60: raise ValueError('Worker evidence is too old')
            count = phase['recoveries'].get(phase['stage'], 0) + 1
            phase['recoveries'][phase['stage']] = count
            phase.update(state='queued' if count <= 2 else 'blocked', token=None, leaseUntil=0, worker=None,
                         error={'kind': 'process', 'message': 'Worker terminated; checkpoint retained', 'evidence': evidence})
            self.save_phase(phase)
            self.event('stage.recovery', phase['batch'], job_id, stage=phase['stage'], attempt=count, state=phase['state'])
            self.refresh_batch(phase['batch'])

    def refresh_batch(self, batch_id):
        batch = self.batch(batch_id)
        if batch['state'] != 'running': return
        # A deduplicated item may be owned by another batch. Its unfinished work
        # still prevents this batch from claiming completion.
        states = [self.phase(item['job'])['state'] for item in batch['items']]
        if all(state == 'done' for state in states):
            batch['state'] = 'complete'
        elif states and all(state in ('done', 'blocked', 'cancelled') for state in states):
            batch['state'] = 'needs_attention'
        else: return
        self.save_batch(batch)
        self.event('batch.' + batch['state'], batch_id)

    def status(self, batch_id):
        batch = self.batch(batch_id)
        items = []
        for item in batch['items']:
            phase = self.phase(item['job'])
            items.append({**item, 'stage': phase['stage'], 'state': phase['state'], 'error': phase['error'],
                          'repairRound': phase['repairRound'], 'budget': self.budgets.status(item['job']),
                          'cumulativePaperBudget':self.budgets.status(self.get(item['job']).get('budgetScope',item['job'])),
                          'problemPackage':phase.get('problemPackage')})
        return {'batch': batch_id, 'title': batch['title'], 'state': batch['state'],
                'summaryHash': batch['summaryHash'], 'confirmed': bool(batch['confirmation']),
                'items': items, 'batchCap': None,
                'pilotBudget': self.budgets.status('pilot:first') if batch['pilot'] else None}

    def read_events(self, after=0, batch_id=None, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid event cursor or limit')
        query = 'SELECT id,batch_id,job_id,kind,created,body FROM workflow_events WHERE id>?'
        args = [after]
        if batch_id is not None: query += ' AND batch_id=?'; args.append(batch_id)
        rows = self.db.execute(query + ' ORDER BY id LIMIT ?', [*args, limit]).fetchall()
        events = [{'id': r[0], 'batch': r[1], 'job': r[2], 'kind': r[3], 'at': r[4], 'data': json.loads(r[5])} for r in rows]
        return {'events': events, 'cursor': rows[-1][0] if rows else after}
