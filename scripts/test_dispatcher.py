import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dispatcher import Queue, drain, execute_job, runner_lock, validate_tasks


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.q = Queue(self.root / 'queue.db')

    def test_manifest_and_copy(self):
        for invalid in (None, (), [], [None], [{'id': 'a'}], [{'id': ' ', 'prompt': 'x'}],
                        [{'id': 'a', 'prompt': 'x', 'worker': None}], [{'id': 'a', 'prompt': 'x', 'extra': 1}],
                        [{'id': 'a', 'prompt': 'x'}, {'id': 'a', 'prompt': 'y'}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_tasks(invalid)
        tasks = [{'id': 'a', 'prompt': ' x '}]
        result = validate_tasks(tasks)
        self.assertEqual(result, tasks)
        self.assertIsNot(result[0], tasks[0])

    def test_atomic_add_and_shared_routing(self):
        self.q.add([{'id': 'a', 'prompt': 'x', 'worker': '21'}])
        with self.assertRaises(sqlite3.IntegrityError):
            self.q.add([{'id': 'b', 'prompt': 'y'}, {'id': 'a', 'prompt': 'z'}])
        self.assertEqual([r['id'] for r in self.q.status()], ['a'])
        self.assertEqual(self.q.claim('5')['id'], 'a')

    def test_uncertain_pauses_only_one_host_and_recovery(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(3)])
        self.q.claim('21')
        with self.assertRaises(ValueError):
            self.q.unblock('21')
        self.q.recover()
        self.assertIsNone(self.q.claim('21'))
        self.assertEqual(self.q.result('0')['status'], 'uncertain')
        self.assertEqual(self.q.claim('5')['id'], '1')
        self.q.unblock('21')
        self.assertEqual(self.q.claim('21')['id'], '2')

    def test_single_runner_and_exception_release(self):
        lock = self.root / 'runner.lock'
        with runner_lock(lock):
            with self.assertRaises(RuntimeError):
                with runner_lock(lock):
                    pass
        with self.assertRaises(ValueError):
            with runner_lock(lock):
                raise ValueError('test')
        with runner_lock(lock):
            pass

    def test_execute_status_and_prompt_cleanup(self):
        cases = [(0, {'complete': True, 'content': 'ok'}, 'done'),
                 (3, {'complete': False, 'content': 'partial'}, 'incomplete'),
                 (0, {'complete': True, 'content': ' '}, 'uncertain'),
                 (1, None, 'uncertain')]
        prompt_dir = self.root / 'prompts'; prompt_dir.mkdir()
        for code, payload, expected in cases:
            completed = subprocess.CompletedProcess([], code, json.dumps(payload), '')
            with patch('dispatcher.subprocess.run', return_value=completed):
                status, _ = execute_job({'id': 'x', 'prompt': 'test'}, '21', prompt_dir, 'm', 100, 1, 'off')
            self.assertEqual(status, expected)
            self.assertEqual(list(prompt_dir.iterdir()), [])
        with patch('dispatcher.subprocess.run', side_effect=subprocess.TimeoutExpired('client', 1)):
            status, _ = execute_job({'id': 'x', 'prompt': 'test'}, '5', prompt_dir, 'm', 100, 1, 'off')
        self.assertEqual(status, 'uncertain')
        self.assertEqual(list(prompt_dir.iterdir()), [])

    def test_three_slots_each_refill_before_slowest_finishes(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(18)])
        active = {'21': 0, '5': 0}; peaks = active.copy()
        started = {}; finished = {}; guard = threading.Lock()
        barrier = threading.Barrier(6, timeout=5)
        def fake(job, worker, *args):
            with guard:
                active[worker] += 1
                peaks[worker] = max(peaks[worker], active[worker])
                started[job['id']] = time.monotonic()
            if int(job['id']) < 6:
                barrier.wait()
            time.sleep(.25 if job['id'] == '0' else .02)
            with guard:
                active[worker] -= 1
                finished[job['id']] = time.monotonic()
            return 'done', {'content': 'ok'}
        with contextlib.redirect_stdout(io.StringIO()):
            result = drain(self.q, self.root, execute=fake)
        self.assertEqual(result, 0)
        self.assertEqual(peaks, {'21': 3, '5': 3})
        self.assertEqual(len(started), 18)
        self.assertLess(started['6'], finished['0'])

    def test_watch_picks_new_jobs_after_idle(self):
        calls = []
        def fake(job, worker, *args):
            calls.append(job['id'])
            return 'done', {}
        # A child runner lets this test terminate a persistent watcher without
        # using or contacting either real host.
        script = '''import time,threading
from pathlib import Path
from queue_store import Queue
from dispatcher import drain
q=Queue(r"%s")
def fake(job,*args): return 'done',{}
t=threading.Thread(target=lambda:drain(q,Path(r"%s"),watch=True,execute=fake),daemon=True)
t.start()
time.sleep(.7)
q.add([{'id':'after-idle','prompt':'x'}])
deadline=time.monotonic()+5
while time.monotonic()<deadline:
 if q.result('after-idle')['status']=='done':
  import os
  os._exit(0)
 time.sleep(.05)
import os
os._exit(1)
''' % (self.root / 'watch.db', self.root)
        import sys
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).parent,
                                capture_output=True, timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
