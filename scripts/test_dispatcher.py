import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch
from dispatcher import Queue, auto_resume_worker, drain, execute_job, render_config_page, runner_lock, should_block_worker, status_summary, validate_tasks, worker_health, worker_warmup
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

    def test_archive_preserves_full_jobs_and_leaves_other_work_unchanged(self):
        self.q.add([{'id': 'reviewed', 'prompt': 'source\nтекст'},
                    {'id': 'other-project-running', 'prompt': 'other'},
                    {'id': 'other-project-queued', 'prompt': 'queued'}])
        self.q.claim('21')
        self.q.finish('reviewed', 'done', {'content': 'patch\nкод', 'extra': [1, None]})
        self.q.claim('33')
        original = self.q.result('reviewed')
        others = [self.q.result(i) for i in ('other-project-running', 'other-project-queued')]
        workers = self.q.workers()
        outcome = self.q.archive([{'id': 'reviewed', 'status': 'accepted', 'reason': 'Applied module fix; focused checks passed'}])
        self.assertEqual(outcome, {'archived': 1, 'ids': ['reviewed']})
        archived = self.q.result('reviewed')
        self.assertEqual({key: archived[key] for key in original}, original)
        self.assertEqual(archived['acceptance_status'], 'accepted')
        self.assertEqual(archived['acceptance_reason'], 'Applied module fix; focused checks passed')
        self.assertGreater(archived['archived_at'], 0)
        self.assertEqual([self.q.result(i) for i in ('other-project-running', 'other-project-queued')], others)
        self.assertEqual(self.q.workers(), workers)
        self.assertEqual([row['id'] for row in self.q.status()], ['other-project-running', 'other-project-queued'])
        self.assertEqual(status_summary(self.q)['counts'], {'running': 1, 'queued': 1})
        self.assertEqual(self.q.export_results(), [])

    def test_archive_rolls_back_whole_batch_for_running_queued_or_missing_job(self):
        self.q.add([{'id': i, 'prompt': i} for i in ('done', 'running', 'queued')])
        self.q.claim('21')
        self.q.finish('done', 'done', {'content': 'result'})
        self.q.claim('33')
        before = [self.q.result(i) for i in ('done', 'running', 'queued')]
        for invalid in ('running', 'queued', 'missing'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.q.archive([{'id': 'done', 'status': 'accepted', 'reason': 'verified'},
                                {'id': invalid, 'status': 'rejected', 'reason': 'test'}])
            self.assertEqual([self.q.result(i) for i in ('done', 'running', 'queued')], before)
            with closing(self.q.connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM archived_jobs').fetchone()[0], 0)

    def test_archive_rolls_back_after_a_write_failure(self):
        self.q.add([{'id': i, 'prompt': i} for i in ('first', 'second')])
        for job_id in ('first', 'second'):
            self.q.claim('21')
            self.q.finish(job_id, 'done', {'content': job_id})
        before = self.q.status()
        with closing(self.q.connect()) as db, db:
            db.execute("CREATE TRIGGER fail_second_archive BEFORE INSERT ON archived_jobs "
                       "WHEN NEW.id='second' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.q.archive([{'id': i, 'status': 'accepted', 'reason': 'verified'}
                            for i in ('first', 'second')])
        self.assertEqual(self.q.status(), before)
        for job_id in ('first', 'second'):
            self.assertEqual(self.q.result(job_id)['result'], {'content': job_id})
        with closing(self.q.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archived_jobs').fetchone()[0], 0)

    def test_archived_id_stays_reserved_and_batch_add_is_atomic(self):
        self.q.add([{'id': 'old', 'prompt': 'source'}])
        self.q.claim('21')
        self.q.finish('old', 'done', {'content': 'result'})
        self.q.archive([{'id': 'old', 'status': 'duplicate', 'reason': 'Covered by verified change'}])
        reopened = Queue(self.q.path)
        with self.assertRaises(sqlite3.IntegrityError):
            reopened.add([{'id': 'new', 'prompt': 'new'}, {'id': 'old', 'prompt': 'repeat'}])
        self.assertEqual(reopened.status(), [])
        self.assertEqual(reopened.result('old')['prompt'], 'source')
        # Old clients inserting directly are covered by the database trigger too.
        with self.assertRaises(sqlite3.IntegrityError), closing(reopened.connect()) as db, db:
            db.execute("INSERT INTO jobs VALUES ('old','retry',NULL,'queued',NULL,0)")

    def test_archive_validation_and_all_terminal_states(self):
        entry = {'id': 'x', 'status': 'rejected', 'reason': 'unsupported'}
        invalid = [None, [], [None], [dict(entry, extra=True)], [dict(entry, reason=' ')],
                   [dict(entry, status='done')], [dict(entry, id=1)], [entry, entry]]
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                self.q.archive(manifest)
        self.q.add([{'id': status, 'prompt': status} for status in ('done', 'incomplete', 'uncertain')])
        entries = []
        for status in ('done', 'incomplete', 'uncertain'):
            self.q.claim('21')
            self.q.finish(status, status, {'content': status}, block_worker=False)
            entries.append({'id': status, 'status': 'rejected', 'reason': 'Reviewed; no applicable change'})
        self.q.archive(entries)
        self.assertEqual(self.q.status(), [])
        for status in ('done', 'incomplete', 'uncertain'):
            self.assertEqual(self.q.result(status)['status'], status)

    def test_archive_cli_and_archived_result_lookup_without_runner(self):
        cli_queue = Queue(self.root / 'queue.sqlite3')
        cli_queue.add([{'id': 'reviewed', 'prompt': 'private source'}])
        cli_queue.claim('21')
        cli_queue.finish('reviewed', 'done', {'content': 'retained result'})
        manifest = self.root / 'acceptance.json'
        manifest.write_text(json.dumps([{'id': 'reviewed', 'status': 'stale', 'reason': 'Superseded source'}]), encoding='utf-8')
        command = [sys.executable, str(Path(__file__).with_name('dispatcher.py')), '--home', str(self.root)]
        archived = subprocess.run(command + ['archive', '--file', str(manifest)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(archived.stdout), {'archived': 1, 'ids': ['reviewed']})
        found = subprocess.run(command + ['result', 'reviewed'], capture_output=True, text=True, check=True)
        output = json.loads(found.stdout)
        self.assertNotIn('prompt', output)
        self.assertEqual(output['result'], {'content': 'retained result'})
        self.assertEqual(output['acceptance_status'], 'stale')
        self.assertFalse((self.root / 'runner.log').exists())

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

    def test_auto_resume_waits_for_stable_model_visibility_before_unblock(self):
        self.q.add([{'id': str(i), 'prompt': 'x'} for i in range(2)])
        job = self.q.claim('21')
        self.q.finish(
            job['id'],
            'uncertain',
            {'error': 'Local API or input file unavailable: Remote end closed connection without response'},
            block_worker=True,
        )
        self.assertTrue(self.q.worker_blocked('21'))
        worker = {
            'id': '21',
            'name': 'Main',
            'base_url': 'http://192.168.88.21:1234/v1',
            'slots': 12,
            'enabled': True,
            'model': 'qwen3.8-9b-distill@q4_k_m',
        }
        missing = subprocess.CompletedProcess([], 0, json.dumps({'models': ['other-model']}), '')
        streak = {}
        with patch('dispatcher.subprocess.run', return_value=missing):
            self.assertFalse(auto_resume_worker(self.q, worker, 'qwen3.8-9b-distill', streak, timeout=1))
        self.assertTrue(self.q.worker_blocked('21'))
        self.assertIn('Waiting for stable /models', self.q.workers()[0]['note'])
        present = subprocess.CompletedProcess([], 0, json.dumps({'models': ['qwen3.8-9b-distill@q4_k_m']}), '')
        with patch('dispatcher.subprocess.run', return_value=present):
            self.assertFalse(auto_resume_worker(self.q, worker, 'qwen3.8-9b-distill', streak, timeout=1))
            self.assertFalse(auto_resume_worker(self.q, worker, 'qwen3.8-9b-distill', streak, timeout=1))
            self.assertTrue(auto_resume_worker(self.q, worker, 'qwen3.8-9b-distill', streak, timeout=1))
        self.assertFalse(self.q.worker_blocked('21'))
        self.assertEqual(self.q.claim('21')['id'], '1')

    def test_auto_resume_does_not_unblock_worker_with_running_job(self):
        self.q.add([{'id': '0', 'prompt': 'x'}])
        self.q.claim('21')
        with closing(self.q.connect()) as db, db:
            db.execute("UPDATE workers SET blocked=1,note='manual test' WHERE id='21'")
        worker = {
            'id': '21',
            'name': 'Main',
            'base_url': 'http://192.168.88.21:1234/v1',
            'slots': 12,
            'enabled': True,
            'model': 'qwen3.8-9b-distill@q4_k_m',
        }
        present = subprocess.CompletedProcess([], 0, json.dumps({'models': ['qwen3.8-9b-distill@q4_k_m']}), '')
        with patch('dispatcher.subprocess.run', return_value=present):
            self.assertFalse(auto_resume_worker(self.q, worker, 'qwen3.8-9b-distill', timeout=1))
        self.assertTrue(self.q.worker_blocked('21'))

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
