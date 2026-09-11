"""Durable text-only worker queue. No model output is executed."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, worker TEXT, status TEXT NOT NULL, result TEXT, created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS archived_jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, worker TEXT, status TEXT NOT NULL, result TEXT, created REAL, archived_at REAL NOT NULL, acceptance_status TEXT NOT NULL, acceptance_reason TEXT NOT NULL)')
            # A database guard also reserves IDs for older clients still running.
            db.execute("CREATE TRIGGER IF NOT EXISTS reserve_archived_job_ids BEFORE INSERT ON jobs WHEN EXISTS (SELECT 1 FROM archived_jobs WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT, 'Job ID is reserved in archive'); END")
            db.execute('CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, blocked INTEGER NOT NULL DEFAULT 0, note TEXT)')
            db.executemany('INSERT OR IGNORE INTO workers(id) VALUES(?)', [('21',), ('33',)])

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def add(self, tasks):
        with closing(self.connect()) as db, db:
            db.executemany('INSERT INTO jobs VALUES (?,?,?,\'queued\',NULL,?)',
                           [(t['id'], t['prompt'], None, time.time()) for t in tasks])

    def sync_workers(self, workers):
        ids = [worker['id'] for worker in workers]
        placeholders = ','.join('?' for _ in ids)
        with closing(self.connect()) as db, db:
            db.executemany(
                'INSERT OR IGNORE INTO workers(id,blocked,note) VALUES(?,0,NULL)',
                [(worker_id,) for worker_id in ids],
            )
            db.execute(
                f"DELETE FROM workers WHERE id NOT IN ({placeholders}) AND id NOT IN "
                "(SELECT worker FROM jobs WHERE status='running' AND worker IS NOT NULL)",
                ids,
            )

    def archive(self, entries):
        """Atomically remove explicitly reviewed terminal jobs from the active queue."""
        if not isinstance(entries, list) or not entries:
            raise ValueError('Expected a nonempty acceptance manifest')
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'id', 'status', 'reason'}:
                raise ValueError('Archive entries require exactly id, status and reason')
            if any(not isinstance(entry[k], str) or not entry[k].strip() for k in entry):
                raise ValueError('Archive id, status and reason must be nonempty strings')
            if entry['status'] not in ('accepted', 'rejected', 'duplicate', 'stale'):
                raise ValueError('Archive status must be accepted, rejected, duplicate or stale')
            if entry['id'] in seen:
                raise ValueError('Duplicate archive ID: ' + entry['id'])
            seen.add(entry['id'])
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = []
            for entry in entries:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (entry['id'],)).fetchone()
                if row is None:
                    raise ValueError('Unknown or already archived job: ' + entry['id'])
                if row['status'] not in ('done', 'incomplete', 'uncertain'):
                    raise ValueError('Cannot archive nonterminal job: ' + entry['id'])
                rows.append((row, entry))
            archived_at = time.time()
            for row, entry in rows:
                db.execute(
                    'INSERT INTO archived_jobs (id,prompt,worker,status,result,created,archived_at,acceptance_status,acceptance_reason) VALUES (?,?,?,?,?,?,?,?,?)',
                    (row['id'], row['prompt'], row['worker'], row['status'], row['result'],
                     row['created'], archived_at, entry['status'], entry['reason']),
                )
                db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
        return {'archived': len(entries), 'ids': [entry['id'] for entry in entries]}

    def claim(self, worker):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            host = db.execute('SELECT blocked FROM workers WHERE id=?', (worker,)).fetchone()
            if not host or host['blocked']:
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',worker=? WHERE id=?", (worker, row['id']))
            return {**dict(row), 'status': 'running', 'worker': worker}

    def finish(self, job_id, status, result, block_worker=True):
        if status not in ('done', 'incomplete', 'uncertain'):
            raise ValueError('Invalid result status')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT worker FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown job')
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, json.dumps(result, ensure_ascii=False), job_id))
            if status == 'uncertain' and block_worker:
                db.execute('UPDATE workers SET blocked=1,note=? WHERE id=?', ('Uncertain request: ' + job_id, row['worker']))

    def status(self):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,worker,status,created FROM jobs ORDER BY created,rowid')]

    def list_jobs(self, prefix='', status='', limit=50):
        sql = 'SELECT id,worker,status,created FROM jobs WHERE 1=1'
        args = []
        if prefix:
            sql += ' AND id LIKE ?'
            args.append(prefix + '%')
        if status:
            sql += ' AND status=?'
            args.append(status)
        sql += ' ORDER BY created,rowid LIMIT ?'
        args.append(limit)
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def export_results(self, prefix='', status=''):
        sql = 'SELECT id,worker,status,result,created FROM jobs WHERE result IS NOT NULL'
        args = []
        if prefix:
            sql += ' AND id LIKE ?'
            args.append(prefix + '%')
        if status:
            sql += ' AND status=?'
            args.append(status)
        sql += ' ORDER BY created,rowid'
        with closing(self.connect()) as db:
            rows = []
            for row in db.execute(sql, args):
                item = dict(row)
                item['result'] = json.loads(item['result']) if item['result'] else None
                rows.append(item)
            return rows

    def workers(self):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM workers ORDER BY id')]

    def worker_blocked(self, worker):
        with closing(self.connect()) as db:
            row = db.execute('SELECT blocked FROM workers WHERE id=?', (worker,)).fetchone()
            return bool(row and row['blocked'])

    def result(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM archived_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            return result

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE workers SET blocked=1,note='Runner interrupted; confirm upstream stopped' WHERE id IN (SELECT worker FROM jobs WHERE status='running')")
            db.execute("UPDATE jobs SET status='uncertain',result=? WHERE status='running'", (json.dumps({'error': 'Runner interrupted; upstream status unknown'}),))

    def unblock(self, worker, note='Confirmed stopped'):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE status='running' AND worker=?", (worker,)).fetchone():
                raise ValueError('Worker still has running jobs')
            db.execute('UPDATE workers SET blocked=0,note=? WHERE id=?', (note, worker))

    def unblock_if_idle(self, worker, note='Auto-resumed'):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE status='running' AND worker=?", (worker,)).fetchone():
                return False
            updated = db.execute(
                'UPDATE workers SET blocked=0,note=? WHERE id=? AND blocked=1',
                (note, worker),
            ).rowcount
            return updated > 0

    def update_worker_note(self, worker, note):
        with closing(self.connect()) as db, db:
            db.execute(
                'UPDATE workers SET note=? WHERE id=? AND blocked=1',
                (note, worker),
            )
