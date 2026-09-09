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
from dispatcher import Queue, drain, execute_job, render_config_page, runner_lock, should_block_worker, status_summary, validate_tasks, worker_health, worker_warmup
from worker_config import DEFAULT_CONTEXT_WINDOW, DEFAULT_TTL_SECONDS, MIN_MAX_TOKENS, default_config, enabled_workers, load_config, normalize_config, save_config


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
        self.assertEqual(self.q.claim('33')['id'], 'a')

    def test_uncertain_pauses_only_one_host_and_recovery(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(3)])
        self.q.claim('21')
        with self.assertRaises(ValueError):
            self.q.unblock('21')
        self.q.recover()
        self.assertIsNone(self.q.claim('21'))
        self.assertEqual(self.q.result('0')['status'], 'uncertain')
        self.assertEqual(self.q.claim('33')['id'], '1')
        self.q.unblock('21')
        self.assertEqual(self.q.claim('21')['id'], '2')

    def test_job_uncertain_does_not_block_worker_slots(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(2)])
        self.q.claim('21')
        self.q.finish('0', 'uncertain', {'error': 'client timeout'}, block_worker=False)
        self.assertEqual(self.q.claim('21')['id'], '1')
        self.assertFalse(self.q.workers()[0]['blocked'])

    def test_unavailable_api_uncertain_blocks_that_worker(self):
        self.assertFalse(should_block_worker('uncertain', {'error': 'client timeout'}))
        self.assertTrue(should_block_worker(
            'uncertain',
            {'error': 'Local API or input file unavailable: target machine actively refused it'},
        ))
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(3)])
        job = self.q.claim('21')
        self.q.finish(
            job['id'],
            'uncertain',
            {'error': 'No connection could be made because the target machine actively refused it'},
            block_worker=True,
        )
        self.assertIsNone(self.q.claim('21'))
        self.assertEqual(self.q.claim('33')['id'], '1')

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
            status, _ = execute_job({'id': 'x', 'prompt': 'test'}, '33', prompt_dir, 'm', 100, 1, 'off')
        self.assertEqual(status, 'uncertain')
        self.assertEqual(list(prompt_dir.iterdir()), [])

    def test_three_slots_each_refill_before_slowest_finishes(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(18)])
        active = {'21': 0, '33': 0}; peaks = active.copy()
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
        self.assertEqual(peaks, {'21': 3, '33': 3})
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

    def test_worker_config_defaults_and_validation(self):
        config = load_config(self.root)
        self.assertEqual([worker['id'] for worker in config['workers']], ['21', '33'])
        self.assertEqual([worker['slots'] for worker in enabled_workers(config)], [3, 3])
        self.assertEqual(config['max_tokens'], MIN_MAX_TOKENS)
        self.assertEqual(config['context_window'], DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(config['ttl'], DEFAULT_TTL_SECONDS)
        saved = save_config(self.root, {
            'model': 'local-model',
            'max_tokens': 2048,
            'context_window': DEFAULT_CONTEXT_WINDOW,
            'ttl': DEFAULT_TTL_SECONDS,
            'timeout': 240,
            'workers': [
                {'id': '21', 'name': 'Main', 'base_url': 'http://192.168.88.21:1234/v1', 'slots': 2, 'enabled': True},
                {'id': 'lab', 'name': 'Lab', 'base_url': 'http://10.0.0.12:9999/v1', 'slots': 1, 'model': 'lab-model', 'max_tokens': 1024, 'context_window': 120000, 'ttl': 600, 'timeout': 300, 'enabled': True},
                {'id': 'off', 'name': 'Off', 'base_url': 'http://127.0.0.1:1234/v1', 'slots': 0, 'enabled': True},
            ],
        })
        self.assertEqual(saved['model'], 'local-model')
        self.assertEqual(saved['max_tokens'], MIN_MAX_TOKENS)
        self.assertEqual(saved['context_window'], DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(saved['ttl'], DEFAULT_TTL_SECONDS)
        self.assertEqual(saved['workers'][1]['model'], 'lab-model')
        self.assertEqual(saved['workers'][1]['max_tokens'], MIN_MAX_TOKENS)
        self.assertEqual(saved['workers'][1]['context_window'], 120000)
        self.assertEqual(saved['workers'][1]['ttl'], 600)
        self.assertEqual(saved['workers'][1]['timeout'], 300)
        self.assertEqual([worker['id'] for worker in enabled_workers(saved)], ['21', 'lab'])
        self.q.sync_workers(saved['workers'])
        self.assertIn('lab', [row['id'] for row in self.q.workers()])
        validate_tasks([{'id': 'new-worker', 'prompt': 'x', 'worker': 'lab'}], {'21', 'lab'})
        with self.assertRaises(ValueError):
            normalize_config({'workers': [{'id': 'bad', 'base_url': 'https://example.com/v1', 'slots': 1, 'enabled': True}]})

    def test_config_ui_labels_context_and_output_separately(self):
        html = render_config_page(default_config())
        self.assertIn('Max output tokens', html)
        self.assertIn('Context window', html)
        self.assertIn('TTL seconds', html)
        self.assertIn(str(MIN_MAX_TOKENS), html)
        self.assertIn(str(DEFAULT_CONTEXT_WINDOW), html)
        self.assertIn(str(DEFAULT_TTL_SECONDS), html)

    def test_sync_workers_removes_stale_idle_workers(self):
        self.q.sync_workers([{'id': '21'}, {'id': '5'}])
        self.assertIn('5', [row['id'] for row in self.q.workers()])
        self.q.sync_workers([{'id': '21'}, {'id': '33'}])
        self.assertNotIn('5', [row['id'] for row in self.q.workers()])
        self.assertIn('33', [row['id'] for row in self.q.workers()])

    def test_custom_worker_slots_are_used(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(5)])
        seen = []
        def fake(job, worker, *args):
            seen.append(worker)
            time.sleep(.02)
            return 'done', {'content': 'ok'}
        workers = [
            {'id': '21', 'base_url': 'http://192.168.88.21:1234/v1', 'slots': 1},
            {'id': '33', 'base_url': 'http://192.168.88.33:1234/v1', 'slots': 2},
        ]
        self.q.sync_workers(workers)
        with contextlib.redirect_stdout(io.StringIO()):
            result = drain(self.q, self.root, execute=fake, workers=workers)
        self.assertEqual(result, 0)
        self.assertEqual(len(seen), 5)
        self.assertIn('21', seen)
        self.assertIn('33', seen)

    def test_per_worker_overrides_and_summary(self):
        self.q.add([{'id': 'pref-1', 'prompt': 'x'}, {'id': 'pref-2', 'prompt': 'x'}])
        calls = []
        def fake(job, worker, directory, model, max_tokens, timeout, reasoning, base_url, ttl, context_window):
            calls.append((worker, model, max_tokens, timeout, base_url, ttl, context_window))
            time.sleep(.02)
            return 'done', {'content': 'ok'}
        workers = [
            {'id': '21', 'base_url': 'http://192.168.88.21:1234/v1', 'slots': 1, 'model': 'fast', 'max_tokens': 512, 'timeout': 60, 'ttl': 600, 'context_window': 120000},
            {'id': '33', 'base_url': 'http://192.168.88.33:1234/v1', 'slots': 1, 'model': '', 'max_tokens': 0, 'timeout': 0},
        ]
        self.q.sync_workers(workers)
        with contextlib.redirect_stdout(io.StringIO()):
            drain(self.q, self.root, execute=fake, workers=workers, model='default', max_tokens=4096, timeout=900)
        self.assertIn(('21', 'fast', MIN_MAX_TOKENS, 60, 'http://192.168.88.21:1234/v1', 600, 120000), calls)
        self.assertIn(('33', 'default', MIN_MAX_TOKENS, 900, 'http://192.168.88.33:1234/v1', DEFAULT_TTL_SECONDS, DEFAULT_CONTEXT_WINDOW), calls)
        summary = status_summary(self.q, prefix='pref-', limit=1)
        self.assertEqual(summary['counts'], {'done': 2})
        self.assertEqual(summary['prefix'], 'pref-')

    def test_worker_health_parses_models(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({'models': ['m1', 'm2']}), '')
        workers = [{'id': '21', 'name': 'Main', 'base_url': 'http://192.168.88.21:1234/v1', 'slots': 3, 'enabled': True}]
        with patch('dispatcher.subprocess.run', return_value=completed):
            result = worker_health(workers, timeout=1)
        self.assertTrue(result[0]['ok'])
        self.assertEqual(result[0]['models'], ['m1', 'm2'])
        with patch('dispatcher.subprocess.run', side_effect=subprocess.TimeoutExpired('client', 1)):
            result = worker_health(workers, timeout=1)
        self.assertFalse(result[0]['ok'])
        self.assertEqual(result[0]['models'], [])

    def test_worker_warmup_parses_output(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({'complete': True, 'model': 'm', 'content': 'OK', 'ttl': 900}), '')
        workers = [{'id': '33', 'name': 'Bionic 33', 'base_url': 'http://192.168.88.33:1234/v1', 'slots': 3, 'enabled': True}]
        with patch('dispatcher.subprocess.run', return_value=completed):
            result = worker_warmup(workers, 'm', DEFAULT_CONTEXT_WINDOW, DEFAULT_TTL_SECONDS, timeout=1)
        self.assertTrue(result[0]['ok'])
        self.assertEqual(result[0]['ttl'], DEFAULT_TTL_SECONDS)


if __name__ == '__main__':
    unittest.main()
