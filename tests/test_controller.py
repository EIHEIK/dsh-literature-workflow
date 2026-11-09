import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from batches import BatchStore
from controller import Controller
from control_api import ControlAPI
from core import digest
from instance_lock import InstanceLock
from local_service import LocalService
from test_workflow import pdf


class FixtureRunner:
    """Offline lifecycle fixture, deliberately not a production PDF runner."""
    def __init__(self): self.workers = {}; self.starts = []; self.cancelled = []; self.lose_ack = False
    def identity(self, phase): return 'fixture-' + phase['job'] + '-' + str(phase['generation'])
    def start(self, phase, job):
        name = phase['worker']
        if name in self.workers: raise ValueError('duplicate worker')
        self.starts.append(name)
        self.workers[name] = {'state': 'running', 'worker': name}
        if phase['stage'] == 'background':
            path = Path(job['source']).parent / 'fixture-background.json'
            path.write_text('{"fixture":true}', encoding='utf-8')
            self.workers[name] = {'state': 'completed', 'worker': name,
                                  'artifacts': {'background': {'path': str(path), 'sha256': digest(path)}}}
        if self.lose_ack: raise TimeoutError('spawn acknowledgement lost')
    def inspect(self, phase):
        return {**self.workers.get(phase['worker'], {'state': 'missing'}),
                'worker': phase['worker'], 'checkedAt': time.time()}
    def cancel(self, phase):
        self.cancelled.append(phase.get('worker'))
        self.workers.pop(phase.get('worker'), None)
        return {'state': 'missing', 'worker': phase.get('worker'), 'checkedAt': time.time()}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.test-output')
        self.root = Path(self.tmp.name); source = self.root / 'source.pdf'; pdf(source)
        self.store = BatchStore(self.root / 'data')
        self.inputs = [{'source': str(source), 'title': 'Fixture'}]
        self.summary = self.store.prepare(self.inputs, {'revision': 'fixture'})
        self.batch = self.summary['batch']; self.job = self.summary['items'][0]['job']
        self.runner = FixtureRunner()
        self.controller = Controller(self.root / 'data', self.runner, min_free_bytes=0, poll_seconds=.02)

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def start_batch(self):
        self.store.confirm(self.batch, self.summary['summaryHash'], 'fixture-user')
        self.store.start(self.batch, self.summary['summaryHash'])

    def test_lost_spawn_ack_is_inspected_without_duplicate_launch(self):
        self.start_batch(); self.runner.lose_ack = True
        self.controller.tick(self.store)
        self.controller.tick(self.store)
        self.assertEqual(len(self.runner.starts), 1)
        self.assertEqual(self.store.phase(self.job)['stage'], 'translate')

    def test_restart_reattaches_live_worker_after_expired_lease(self):
        self.start_batch()
        self.controller.tick(self.store); self.controller.tick(self.store); self.controller.tick(self.store)
        with self.store.transaction():
            phase = self.store.phase(self.job); phase['leaseUntil'] = 1; self.store.save_phase(phase)
        second = Controller(self.root / 'data', self.runner, min_free_bytes=0)
        second.tick(self.store)
        self.assertEqual(len(self.runner.starts), 2)
        self.assertGreater(self.store.phase(self.job)['leaseUntil'], time.time())

    def test_cancel_targets_original_worker_despite_generation_invalidation(self):
        self.start_batch(); self.controller.tick(self.store)
        original = self.store.phase(self.job)['worker']
        self.store.control(self.batch, 'cancel'); self.controller.tick(self.store)
        self.assertEqual(self.runner.cancelled, [original])
        self.assertTrue(self.store.phase(self.job)['terminationVerified'])

    def test_observation_unavailable_does_not_recover_or_spawn(self):
        self.start_batch(); self.controller.tick(self.store)
        original = self.store.phase(self.job)
        with patch.object(self.runner, 'inspect', return_value={'state': 'unavailable'}):
            self.controller.tick(self.store)
        self.assertEqual(self.store.phase(self.job), original)
        self.assertEqual(len(self.runner.starts), 1)

    def test_budget_block_stops_own_worker_without_restarting_it(self):
        self.start_batch();self.controller.tick(self.store)
        phase=self.store.phase(self.job);original=phase['worker']
        self.store.fail(self.job,phase['token'],'budget','Offline exhausted-budget fixture')
        self.controller.tick(self.store);self.controller.tick(self.store)
        self.assertEqual(self.runner.cancelled,[original])
        self.assertEqual(len(self.runner.starts),1)
        self.assertTrue(self.store.phase(self.job)['terminationVerified'])

    def test_invalid_stage_output_blocks_paper_without_crashing_controller(self):
        self.start_batch();self.controller.tick(self.store)
        with patch.object(self.runner,'inspect',return_value={'state':'failed','kind':'evidence','message':'Invalid fixture result'}):
            self.controller.tick(self.store)
        self.assertEqual(self.store.phase(self.job)['state'],'blocked')
        self.assertIsNone(self.controller.error)
        self.controller.tick(self.store)
        self.assertEqual(len(self.runner.cancelled),1)

    def test_storage_shortage_preserves_queue_and_recovers(self):
        self.start_batch(); self.controller.min_free_bytes = 100
        with patch('controller.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            self.controller.tick(self.store); self.controller.tick(self.store)
        self.assertEqual(self.runner.starts, [])
        kinds = [e['kind'] for e in self.store.read_events()['events']]
        self.assertEqual(kinds.count('controller.storage_blocked'), 1)
        self.controller.tick(self.store)
        self.assertEqual(len(self.runner.starts), 1)

    def test_docker_host_disk_shortage_does_not_consume_recovery_attempts(self):
        self.start_batch()
        self.runner.storage_ready=lambda:False
        self.controller.tick(self.store);self.controller.tick(self.store)
        self.assertEqual(self.runner.starts,[])
        self.assertEqual(self.store.phase(self.job)['recoveries'],{})
        self.runner.storage_ready=lambda:True
        self.controller.tick(self.store)
        self.assertEqual(len(self.runner.starts),1)

    def test_os_lock_prevents_second_controller_and_releases(self):
        with InstanceLock(self.root / 'data'):
            with self.assertRaisesRegex(RuntimeError, 'Another controller'):
                with InstanceLock(self.root / 'data'): pass
        with InstanceLock(self.root / 'data'): pass

    def test_control_api_refuses_arbitrary_paths_budget_and_confirm(self):
        api = ControlAPI(lambda scope, pages: (self.inputs, []), {'revision': 'fixture'})
        for method, args in [('confirm_batch', {}), ('prepare_batch', {'scope': {'kind':'items','keys':['ABCDEFGH']}, 'source':'C:/private.pdf'}),
                             ('start_batch', {'batch':self.batch,'summaryHash':self.summary['summaryHash'],'budget':999})]:
            with self.assertRaises(ValueError): api.call(self.store, method, args)

    def test_unvalidated_deployment_cannot_start_even_confirmed_batch(self):
        api = ControlAPI(lambda scope, pages: (self.inputs, []), {'revision':'fixture'})
        self.store.confirm(self.batch,self.summary['summaryHash'],'fixture-user')
        self.assertFalse(api.call(self.store,'get_capabilities',{})['productionReady'])
        with self.assertRaisesRegex(ValueError,'readiness'):
            api.call(self.store,'start_batch',{'batch':self.batch,'summaryHash':self.summary['summaryHash']})

    def test_background_loop_and_authenticated_local_surface(self):
        credentials = {role: secrets.token_urlsafe(32) for role in ('dsh','dot','confirmation','maintenance')}
        api = ControlAPI(lambda scope, pages: (self.inputs, []), {'revision': 'fixture'}, readiness=lambda:True)
        thread = threading.Thread(target=self.controller.run); thread.start()
        self.assertTrue(self.controller.ready.wait(5))
        service = LocalService(self.controller, api, credentials); service.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def call(role, payload, endpoint='/rpc', origin=None):
            headers = {'Authorization':'Bearer '+credentials[role], 'Content-Type':'application/json'}
            if origin: headers['Origin'] = origin
            req = urllib.request.Request(service.url + endpoint, json.dumps(payload).encode(), headers)
            with opener.open(req, timeout=5) as response: return json.load(response)['result']
        status = {'method':'get_status','arguments':{'batch':self.batch}}
        try:
            self.assertEqual(call('dsh', status)['state'], 'awaiting_confirmation')
            with self.assertRaises(urllib.error.HTTPError) as error: call('dot', status)
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError): call('dsh', status, origin='https://untrusted.example')
            confirmation = {'batch':self.batch,'summaryHash':self.summary['summaryHash'],'receipt':'synthetic-host-confirmation'}
            with self.assertRaises(urllib.error.HTTPError): call('dsh', confirmation, '/confirmation')
            with self.assertRaises(urllib.error.HTTPError): call('maintenance', confirmation, '/confirmation')
            with self.assertRaises(urllib.error.HTTPError): call('dsh', {'action':'stop'}, '/maintenance/stop')
            with self.assertRaises(urllib.error.HTTPError): call('maintenance', status)
            call('confirmation', confirmation, '/confirmation')
            call('dsh', {'method':'start_batch','arguments':{'batch':self.batch,'summaryHash':self.summary['summaryHash']}})
            # Disconnect the control surface; the independent loop keeps working.
            service.close(); service = None
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                result = self.controller.submit(lambda store: store.status(self.batch))
                if result['items'][0]['stage']=='translate': break
                time.sleep(.02)
            self.assertEqual(result['items'][0]['stage'], 'translate')
            self.assertIsNone(self.controller.error)
        finally:
            if service: service.close()
            self.controller.stop(); thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__': unittest.main()
