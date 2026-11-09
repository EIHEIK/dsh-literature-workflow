"""Single-writer orchestration loop, independent of a chat or MCP connection.

The runner is trusted deployment code, never supplied by a tool caller. It must
name workers deterministically from job/stage/generation, so a crash between
spawn and receipt persistence cannot cause a second worker to be launched.
No production runner is selected implicitly.
"""
from concurrent.futures import Future
import json
import queue
import shutil
import threading
import time

from batches import BatchStore
from instance_lock import InstanceLock


class Controller:
    def __init__(self, root, runner, *, min_free_bytes=2*1024**3, poll_seconds=1, broker_factory=None,startup_hook=None,dispatch_guard=None):
        self.root = root
        self.runner = runner
        self.min_free_bytes = min_free_bytes
        self.poll_seconds = poll_seconds
        self.commands = queue.Queue(maxsize=128)
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.storage_blocked = False
        self.broker_factory = broker_factory
        self.startup_hook = startup_hook
        self.dispatch_guard = dispatch_guard
        self.broker_pump = None

    def submit(self, operation, timeout=30):
        """Trusted adapters enqueue bounded operations; they never share SQLite."""
        if self.stopping.is_set(): raise RuntimeError('Controller is stopping')
        future = Future()
        self.commands.put_nowait((operation, future))
        return future.result(timeout=timeout)

    def run(self):
        try:
            with InstanceLock(self.root): self._run_owned()
        except BaseException as error:
            self.error = type(error).__name__
            self.stopping.set(); self.ready.set()
            raise

    def _run_owned(self):
        store = None
        try:
            store = BatchStore(self.root)
            if hasattr(self.runner,'bind'): self.runner.bind(store)
            if self.broker_factory is not None:
                self.broker_pump = self.broker_factory(store)
                self.broker_pump.broker.recover_controller_restart()
            if self.startup_hook is not None:self.startup_hook(store)
            self.ready.set()
            next_tick = 0
            while not self.stopping.is_set():
                try:
                    operation, future = self.commands.get(timeout=self.poll_seconds)
                    if future.set_running_or_notify_cancel():
                        try: future.set_result(operation(store))
                        except Exception as error: future.set_exception(error)
                except queue.Empty:
                    pass
                if time.monotonic() >= next_tick:
                    self.tick(store)
                    next_tick = time.monotonic() + self.poll_seconds
        except BaseException as error:
            self.error = type(error).__name__
            raise
        finally:
            self.stopping.set(); self.ready.set()
            try:
                if self.broker_pump is not None: self.broker_pump.close()
            finally:
                if store is not None: store.db.close()
            while True:
                try: _, future = self.commands.get_nowait()
                except queue.Empty: break
                if not future.done(): future.set_exception(RuntimeError('Controller stopped'))

    def stop(self):
        # Existing isolated workers remain identifiable. Next controller inspects
        # them before dispatch; stopping the UI/control service is not a cancel.
        self.stopping.set()

    def tick(self, store):
        if self.dispatch_guard is not None:self.dispatch_guard(store)
        if self.broker_pump is not None:
            self.broker_pump.tick(self.runner.mailbox_scopes(store))
        with store.transaction():
            for batch_id, in store.db.execute('SELECT id FROM batches').fetchall():
                store.refresh_batch(batch_id)
        phases = [json.loads(r[0]) for r in store.db.execute('SELECT body FROM phase_jobs')]
        for phase in phases:
            if phase['state']=='blocked' and not phase.get('problemPackage'):
                from issue_packages import create_package
                try:
                    create_package(store,phase)
                    phase=store.phase(phase['job'])
                except OSError:
                    # Preserve the failure and queue when storage cannot hold a
                    # bundle; the existing source/candidate evidence is retained.
                    pass
            if phase['state'] in ('cancelled','blocked') and phase.get('worker') and not phase.get('terminationVerified'):
                # cancel invalidates the lease first, then kills only this job's
                # deterministic worker. Retain it until terminal evidence exists.
                observed = self.runner.cancel(phase)
                if observed.get('state') in ('terminal', 'missing'):
                    with store.transaction():
                        phase['terminationVerified'] = True
                        store.save_phase(phase)
                        store.event('worker.cancelled' if phase['state']=='cancelled' else 'worker.stopped_after_block', phase['batch'], phase['job'])
                else: return
            if phase['state'] != 'running': continue
            observed = self.runner.inspect(phase)
            state = observed.get('state')
            if state == 'unavailable':
                # Docker/service observation failure is not evidence of absence.
                return
            if state == 'running':
                now = time.time()
                config = store.get(phase['job'])['config']
                timeout = config.get('stageTimeoutSeconds', 3600)
                idle_timeout = config.get('progressTimeoutSeconds', 900)
                progress = observed.get('progressFingerprint')
                new_progress = progress and progress != phase.get('progressFingerprint')
                if (now-phase.get('startedAt', phase['heartbeatAt']) > timeout or
                        (not new_progress and now-phase['progressAt'] > idle_timeout)):
                    stopped = self.runner.cancel(phase)
                    if stopped.get('state') not in ('terminal', 'missing'): return
                    store.recover(phase['job'], phase['token'], stopped)
                    return
                # A live worker can be reattached after controller downtime.
                # It is still the same worker/generation, not a new lease owner.
                with store.transaction():
                    current = store.phase(phase['job'])
                    current['leaseUntil'] = now + 300
                    current['heartbeatAt'] = now
                    if new_progress:
                        current['progressFingerprint'] = progress
                        current['progressAt'] = now
                    store.save_phase(current)
                return
            if state == 'completed':
                # The trusted runner verifies output receipts before reporting
                # completed. Store rechecks all artifact and acceptance hashes.
                with store.transaction():
                    current = store.phase(phase['job'])
                    current['leaseUntil'] = time.time() + 300
                    store.save_phase(current)
                try:
                    store.finish(phase['job'], phase['token'], observed.get('artifacts', {}),
                                 verdict=observed.get('verdict'))
                except (ValueError, OSError, KeyError):
                    store.fail(phase['job'], phase['token'], 'evidence', 'Worker output failed artifact or acceptance validation')
                return
            if state == 'failed':
                store.fail(phase['job'],phase['token'],observed.get('kind','evidence'),
                           observed.get('message','Stage failed validation'))
                return
            if state in ('terminal', 'missing'):
                store.recover(phase['job'], phase['token'], observed)
                return
            # Unknown replies do not authorize a retry or acceptance.
            raise ValueError('Unrecognized runner observation')

        host_storage_ok = not hasattr(self.runner,'storage_ready') or self.runner.storage_ready()
        if shutil.disk_usage(store.root).free < self.min_free_bytes or not host_storage_ok:
            # Keep the queue intact; recover automatically when storage recovers.
            if not self.storage_blocked:
                with store.transaction(): store.event('controller.storage_blocked', minimumFreeBytes=self.min_free_bytes)
                self.storage_blocked = True
            return
        if self.storage_blocked:
            with store.transaction(): store.event('controller.storage_recovered')
            self.storage_blocked = False
        phase = store.claim_next()
        if phase is None: return
        with store.transaction():
            phase['startedAt'] = time.time()
            phase['worker'] = self.runner.identity(phase)
            store.save_phase(phase)
        try:
            self.runner.start(phase, store.get(phase['job']))
        except Exception:
            # Spawn may have succeeded before its acknowledgement was lost.
            # Never mark it absent or launch another here; inspect on next tick.
            with store.transaction():
                store.event('worker.launch_unconfirmed', phase['batch'], phase['job'])

