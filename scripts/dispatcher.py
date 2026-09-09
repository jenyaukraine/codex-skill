"""Reusable dispatcher for the two approved, already running LM Studio workers."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue_store import Queue

DEFAULT_HOME = Path(__file__).resolve().parents[3] / 'bionic-dispatcher'


def validate_tasks(tasks):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Expected a nonempty list of tasks')
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {'id', 'prompt', 'worker'}:
            raise ValueError('Each task must contain only id, prompt, optional worker')
        if any(not isinstance(task.get(k), str) or not task[k].strip() for k in ('id', 'prompt')):
            raise ValueError('id and prompt must be nonempty strings')
        if 'worker' in task and task['worker'] not in ('21', '5'):
            raise ValueError('worker must be 21 or 5')
        if task['id'] in seen:
            raise ValueError('Duplicate task id: ' + task['id'])
        seen.add(task['id'])
    return [dict(t) for t in tasks]


@contextlib.contextmanager
def runner_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another dispatcher is already running') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def execute_job(job, worker, directory, model, max_tokens, timeout, reasoning):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', dir=directory, delete=False) as prompt:
        prompt.write(job['prompt'])
        prompt_path = Path(prompt.name)
    try:
        command = [sys.executable, str(Path(__file__).with_name('client.py')), '--via-dispatcher', '--worker', worker,
                   '--prompt-file', str(prompt_path), '--model', model, '--max-tokens', str(max_tokens),
                   '--timeout', str(timeout)]
        if reasoning in ('off', 'on'):
            command.extend(['--reasoning', reasoning])
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                timeout=timeout + 20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            return 'uncertain', {'error': result.stderr[:1000] or 'Invalid client response', 'exit_code': result.returncode}
        if isinstance(payload, dict):
            if result.returncode == 0 and payload.get('complete') is True and isinstance(payload.get('content'), str) and payload['content'].strip():
                return 'done', payload
            if result.returncode == 3 and payload.get('complete') is False:
                return 'incomplete', payload
        return 'uncertain', {'error': result.stderr[:1000] or 'Unexpected completion status', 'response': payload}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 'uncertain', {'error': str(exc)[:1000], 'retry': False}
    finally:
        prompt_path.unlink(missing_ok=True)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def ensure_runner(directory):
    # Probe the same OS lock used by the runner. A competing start is harmless:
    # only one process can acquire it. Never delete a live lock file.
    try:
        with runner_lock(DEFAULT_HOME / 'runner.lock'):
            pass
    except RuntimeError:
        return 'already-running'
    command = [sys.executable, str(Path(__file__).resolve()), '--home', str(directory), 'run', '--watch']
    with (directory / 'runner.log').open('ab') as log:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         start_new_session=os.name != 'nt', close_fds=True)
    return 'starting'


def drain(queue, directory, slots=3, watch=False, execute=execute_job, model='qwen3.8-9b-distill', max_tokens=4096, timeout=180, reasoning='openai'):
    stop = threading.Event()
    output_lock = threading.Lock()
    def slot(worker):
        while not stop.is_set():
            job = queue.claim(worker)
            if job is None:
                if watch:
                    stop.wait(.5)
                    continue
                return
            with output_lock:
                emit({'event': 'started', 'id': job['id'], 'worker': worker})
            try:
                status, result = execute(job, worker, directory, model, max_tokens, timeout, reasoning)
            except Exception as exc:
                status, result = 'uncertain', {'error': str(exc)[:1000]}
            queue.finish(job['id'], status, result)
            with output_lock:
                emit({'event': status, 'id': job['id'], 'worker': worker})
    with ThreadPoolExecutor(max_workers=2 * slots) as pool:
        futures = [pool.submit(slot, worker) for worker in ('21', '5') for _ in range(slots)]
        try:
            for future in futures:
                future.result()
        except KeyboardInterrupt:
            stop.set()
            emit({'event': 'stopping', 'message': 'Waiting for current requests; no new jobs will start'})
    return 0 if all(j['status'] == 'done' for j in queue.status()) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME, help='Durable queue directory')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add', help='Atomically enqueue a JSON task manifest')
    add.add_argument('--file', type=Path, required=True)
    add.add_argument('--no-start', action='store_true', help='Only enqueue; used for isolated tests')
    run = commands.add_parser('run', help='Fill each free slot immediately from the queue')
    run.add_argument('--slots', type=int, choices=range(1, 5), default=3)
    run.add_argument('--watch', action='store_true', help='Wait for new tasks until Ctrl+C')
    run.add_argument('--model', default='qwen3.8-9b-distill')
    run.add_argument('--max-tokens', type=int, default=4096)
    run.add_argument('--timeout', type=int, default=180)
    run.add_argument('--reasoning', choices=('openai', 'off', 'on'), default='openai')
    commands.add_parser('status')
    result = commands.add_parser('result'); result.add_argument('id')
    unblock = commands.add_parser('unblock'); unblock.add_argument('worker', choices=('21', '5')); unblock.add_argument('--note', required=True)
    args = parser.parse_args()
    args.home.mkdir(parents=True, exist_ok=True)
    queue = Queue(args.home / 'queue.sqlite3')
    if args.command == 'add':
        tasks = validate_tasks(json.loads(args.file.read_text(encoding='utf-8-sig')))
        queue.add(tasks); emit({'added': len(tasks), 'ids': [t['id'] for t in tasks]})
        if not args.no_start:
            emit({'runner': ensure_runner(args.home)})
    elif args.command == 'status':
        emit({'workers': queue.workers(), 'jobs': queue.status()})
    elif args.command == 'result':
        item = queue.result(args.id)
        if item is None:
            raise ValueError('Unknown job: ' + args.id)
        item.pop('prompt'); emit(item)
    elif args.command == 'unblock':
        if not args.note.strip():
            raise ValueError('A confirmation note is required')
        queue.unblock(args.worker, args.note); emit({'unblocked': args.worker, 'note': args.note})
    elif args.command == 'run':
        if args.timeout < 1 or args.max_tokens < 1:
            raise ValueError('Timeout and token budget must be positive')
        # One runner across all queue directories protects the physical worker caps.
        with runner_lock(DEFAULT_HOME / 'runner.lock'):
            queue.recover()
            return drain(queue, args.home, args.slots, args.watch, model=args.model,
                         max_tokens=args.max_tokens, timeout=args.timeout, reasoning=args.reasoning)
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        emit({'error': str(exc)}); raise SystemExit(1)
