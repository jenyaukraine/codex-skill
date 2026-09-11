"""Adapter checks use temporary source files and an isolated dispatcher database."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import source_review as engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.write('components/button.tsx', 'export const Button = () => <button>Привет</button>;')

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_discovery_excludes_dependencies_builds_and_backups(self):
        page = self.write('app/page.tsx', 'export default function Page() {}')
        for name in ('node_modules/pkg/x.tsx', '.backup/app/page.tsx', '.next/app/page.tsx',
                     'src/node_modules/pkg/x.tsx', 'components/.backup/x.tsx'):
            self.write(name, 'ignore')
        self.assertEqual(engine.index_files(self.root), sorted([page, self.source]))

    def test_manifest_contains_source_and_stable_revision_identity(self):
        first = engine.prepare(self.root, [self.source, self.source])
        tasks = json.loads((first / 'tasks.json').read_text(encoding='utf-8'))
        self.assertEqual(len(tasks), 1)
        self.assertIn(self.source.read_text(encoding='utf-8'), tasks[0]['prompt'])
        self.assertIn(engine.digest(self.source.read_bytes()), tasks[0]['prompt'])
        second = engine.prepare(self.root, [self.source])
        self.assertEqual(json.loads((second / 'tasks.json').read_text(encoding='utf-8')), tasks)
        self.source.write_text('changed', encoding='utf-8')
        third = engine.prepare(self.root, [self.source])
        self.assertNotEqual(json.loads((third / 'tasks.json').read_text())[0]['id'], tasks[0]['id'])

    def test_large_files_are_skipped_and_empty_manifest_is_rejected(self):
        large = self.write('components/large.tsx', 'x' * 100)
        with self.assertRaises(ValueError):
            engine.prepare(self.root, [large], max_bytes=10)
        with self.assertRaises(ValueError):
            engine.prepare(self.root, [])

    def test_submit_and_collect_with_real_isolated_dispatcher(self):
        home = self.root / 'isolated-queue'
        original_dispatch = engine.dispatch

        def isolated_dispatch(*args):
            if args[0] == 'add':
                args = (*args, '--no-start')
            return original_dispatch('--home', home, *args)

        with patch.object(engine, 'dispatch', side_effect=isolated_dispatch), patch.object(
                sys, 'argv', ['engine', '--workspace', str(self.root), '--file', 'components/button.tsx']):
            engine.main()
            run = next((self.root / 'bionic_output').iterdir())
            tasks = json.loads((run / 'tasks.json').read_text(encoding='utf-8'))
            # Simulate completion through the real queue API, not the live LAN workers.
            code = ('from queue_store import Queue; import sys; '
                    'q=Queue(sys.argv[1]); job=q.claim("21"); '
                    'assert job["id"] == sys.argv[2]; '
                    'q.finish(job["id"], "done", {"content": "full answer " * 1000})')
            subprocess.run([sys.executable, '-c', code, str(home / 'queue.sqlite3'), tasks[0]['id']],
                           cwd=engine.DISPATCHER.parent, check=True)
            self.source.write_text('new source', encoding='utf-8')
            output = engine.collect(run)
            row = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(row['id'], tasks[0]['id'])
            self.assertEqual(row['result']['content'], 'full answer ' * 1000)
            self.assertTrue(row['source_changed'])
            self.assertEqual(row['review_status'], 'pending')
            self.assertEqual(row['queue_status'], 'done')
            self.assertEqual(self.source.read_text(), 'new source')
            with self.assertRaises(RuntimeError):
                isolated_dispatch('add', '--file', run / 'tasks.json')
            self.assertFalse((home / 'runner.log').exists())

    def test_rejects_mismatched_result_id(self):
        run = engine.prepare(self.root, [self.source])
        with self.assertRaises(ValueError):
            engine.collect(run, lambda task_id: {'id': 'wrong', 'status': 'done', 'result': {}})

    def test_collection_preserves_lookup_failures(self):
        run = engine.prepare(self.root, [self.source])
        def unavailable(task_id):
            raise RuntimeError('Unknown job')
        row = json.loads(engine.collect(run, unavailable).read_text())
        self.assertEqual(row['lookup_error'], 'Unknown job')
        self.assertEqual(row['review_status'], 'pending')


if __name__ == '__main__':
    unittest.main()
