"""Reusable dispatcher for the two approved, already running LM Studio workers."""
import argparse
import contextlib
from html import escape
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
import webbrowser
from queue_store import Queue
from worker_config import default_config, enabled_workers, load_config, save_config

DEFAULT_HOME = Path(__file__).resolve().parents[3] / 'bionic-dispatcher'


def validate_tasks(tasks, worker_ids=None):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Expected a nonempty list of tasks')
    worker_ids = set(worker_ids or ('21', '5'))
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {'id', 'prompt', 'worker'}:
            raise ValueError('Each task must contain only id, prompt, optional worker')
        if any(not isinstance(task.get(k), str) or not task[k].strip() for k in ('id', 'prompt')):
            raise ValueError('id and prompt must be nonempty strings')
        if 'worker' in task and task['worker'] not in worker_ids:
            raise ValueError('worker must be one of: ' + ', '.join(sorted(worker_ids)))
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


def execute_job(job, worker, directory, model, max_tokens, timeout, reasoning, base_url=None):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', dir=directory, delete=False) as prompt:
        prompt.write(job['prompt'])
        prompt_path = Path(prompt.name)
    try:
        command = [sys.executable, str(Path(__file__).with_name('client.py')), '--via-dispatcher',
                   '--prompt-file', str(prompt_path), '--model', model, '--max-tokens', str(max_tokens),
                   '--timeout', str(timeout)]
        if base_url:
            command.extend(['--base-url', base_url])
        else:
            command.extend(['--worker', worker])
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


def drain(queue, directory, slots=3, watch=False, execute=execute_job, model='qwen3.8-9b-distill', max_tokens=4096, timeout=180, reasoning='openai', workers=None):
    if workers is None:
        workers = [{'id': '21', 'base_url': None, 'slots': slots}, {'id': '5', 'base_url': None, 'slots': slots}]
    stop = threading.Event()
    output_lock = threading.Lock()
    def slot(worker):
        worker_id = worker['id']
        while not stop.is_set():
            job = queue.claim(worker_id)
            if job is None:
                if watch:
                    stop.wait(.5)
                    continue
                return
            with output_lock:
                emit({'event': 'started', 'id': job['id'], 'worker': worker_id})
            try:
                status, result = execute(job, worker_id, directory, model, max_tokens, timeout, reasoning, worker.get('base_url'))
            except Exception as exc:
                status, result = 'uncertain', {'error': str(exc)[:1000]}
            queue.finish(job['id'], status, result)
            with output_lock:
                emit({'event': status, 'id': job['id'], 'worker': worker_id})
    max_workers = sum(worker['slots'] for worker in workers)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(slot, worker) for worker in workers for _ in range(worker['slots'])]
        try:
            for future in futures:
                future.result()
        except KeyboardInterrupt:
            stop.set()
            emit({'event': 'stopping', 'message': 'Waiting for current requests; no new jobs will start'})
    return 0 if all(j['status'] == 'done' for j in queue.status()) else 2


def render_config_page(config, message=''):
    worker_rows = []
    for index, worker in enumerate(config['workers']):
        checked = 'checked' if worker['enabled'] else ''
        worker_rows.append(f"""
        <tr>
          <td><input name="id_{index}" value="{escape(worker['id'])}" required></td>
          <td><input name="name_{index}" value="{escape(worker['name'])}" required></td>
          <td><input name="base_url_{index}" value="{escape(worker['base_url'])}" required></td>
          <td><input type="number" min="0" max="8" name="slots_{index}" value="{worker['slots']}" required></td>
          <td><input type="checkbox" name="enabled_{index}" {checked}></td>
        </tr>""")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bionic Local Configuration</title>
<style>
:root {{ --bg:#eef5f1; --panel:#fff; --ink:#10251d; --muted:#64736c; --green:#0d996e; --line:#dfe7e2; }}
body {{ margin:0; font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--ink); }}
main {{ max-width:1100px; margin:32px auto; padding:0 20px; }}
.hero {{ background:#073f31; color:white; border-radius:8px; padding:24px; display:flex; justify-content:space-between; gap:20px; align-items:center; }}
.mark {{ width:48px; height:48px; border-radius:8px; background:#b8f4a8; color:#073f31; display:grid; place-items:center; font-weight:900; font-size:28px; }}
.hero h1 {{ margin:0; font-size:28px; }}
.hero p {{ margin:4px 0 0; color:#d8eee6; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; margin-top:16px; padding:20px; box-shadow:0 16px 40px rgba(16,37,29,.08); }}
.message {{ padding:10px 12px; border-radius:8px; background:#e4f7ec; color:#096445; margin-bottom:14px; }}
label {{ display:block; color:var(--muted); font-weight:700; font-size:12px; margin-bottom:6px; text-transform:uppercase; }}
input {{ box-sizing:border-box; width:100%; min-height:40px; border:1px solid var(--line); border-radius:8px; padding:8px 10px; font:inherit; }}
table {{ width:100%; border-collapse:collapse; margin-top:14px; }}
th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:10px 8px; vertical-align:middle; }}
th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
.grid {{ display:grid; grid-template-columns:2fr 1fr 1fr; gap:12px; }}
button,.button {{ border:0; border-radius:8px; background:var(--green); color:white; padding:11px 16px; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; min-height:42px; align-items:center; }}
.secondary {{ background:#e8efeb; color:var(--ink); }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }}
.hint {{ color:var(--muted); font-size:13px; margin-top:10px; }}
</style></head><body><main>
<section class="hero"><div><h1>Bionic Local</h1><p>Worker configuration for the shared Codex queue.</p></div><div class="mark">B</div></section>
<section class="panel">
{f'<div class="message">{escape(message)}</div>' if message else ''}
<form method="post">
  <div class="grid">
    <div><label>Default model</label><input name="model" value="{escape(config['model'])}" required></div>
    <div><label>Max tokens</label><input type="number" min="1" name="max_tokens" value="{config['max_tokens']}" required></div>
    <div><label>Timeout seconds</label><input type="number" min="1" name="timeout" value="{config['timeout']}" required></div>
  </div>
  <table>
    <thead><tr><th>ID</th><th>Name</th><th>Base URL</th><th>Slots</th><th>Enabled</th></tr></thead>
    <tbody>{''.join(worker_rows)}
      <tr><td><input name="id_new" placeholder="new-id"></td><td><input name="name_new" placeholder="New worker"></td><td><input name="base_url_new" placeholder="http://192.168.88.50:1234/v1"></td><td><input type="number" min="0" max="8" name="slots_new" value="1"></td><td><input type="checkbox" name="enabled_new"></td></tr>
    </tbody>
  </table>
  <div class="actions"><button>Save configuration</button><a class="button secondary" href="/">Reload</a></div>
  <p class="hint">Set slots to 0 or disable a flaky machine. Uncertain running requests still require manual unblock after the upstream generation is stopped.</p>
</form></section></main></body></html>"""


def form_to_config(form):
    workers = []
    indexes = sorted({key.split('_', 1)[1] for key in form if key.startswith('id_')})
    for index in indexes:
        worker_id = form.get('id_' + index, [''])[0].strip()
        base_url = form.get('base_url_' + index, [''])[0].strip()
        if not worker_id and not base_url:
            continue
        workers.append({
            'id': worker_id,
            'name': form.get('name_' + index, [worker_id])[0].strip() or worker_id,
            'base_url': base_url,
            'slots': int(form.get('slots_' + index, ['0'])[0] or 0),
            'enabled': ('enabled_' + index) in form,
        })
    return {
        'model': form.get('model', [''])[0],
        'max_tokens': int(form.get('max_tokens', ['4096'])[0] or 4096),
        'timeout': int(form.get('timeout', ['180'])[0] or 180),
        'workers': workers,
    }


def serve_config_ui(home, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, status, body):
            data = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, render_config_page(load_config(home)))

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0') or 0)
            form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True)
            try:
                config = save_config(home, form_to_config(form))
                message = 'Configuration saved. Restart the dispatcher runner to apply changed worker slots to already-running watch processes.'
            except (ValueError, TypeError) as exc:
                config = load_config(home)
                message = 'Not saved: ' + str(exc)
            self._send(200, render_config_page(config, message))

    server = ThreadingHTTPServer((host, port), Handler)
    url = f'http://{host}:{server.server_port}/'
    print(json.dumps({'config_ui': url}, ensure_ascii=False), flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


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
    run.add_argument('--model')
    run.add_argument('--max-tokens', type=int)
    run.add_argument('--timeout', type=int)
    run.add_argument('--reasoning', choices=('openai', 'off', 'on'), default='openai')
    commands.add_parser('status')
    commands.add_parser('config')
    config_ui = commands.add_parser('config-ui')
    config_ui.add_argument('--host', default='127.0.0.1')
    config_ui.add_argument('--port', type=int, default=8795)
    result = commands.add_parser('result'); result.add_argument('id')
    unblock = commands.add_parser('unblock'); unblock.add_argument('worker'); unblock.add_argument('--note', required=True)
    args = parser.parse_args()
    args.home.mkdir(parents=True, exist_ok=True)
    config = load_config(args.home)
    queue = Queue(args.home / 'queue.sqlite3')
    queue.sync_workers(config['workers'])
    if args.command == 'add':
        tasks = validate_tasks(json.loads(args.file.read_text(encoding='utf-8-sig')), {worker['id'] for worker in config['workers']})
        queue.add(tasks); emit({'added': len(tasks), 'ids': [t['id'] for t in tasks]})
        if not args.no_start:
            emit({'runner': ensure_runner(args.home)})
    elif args.command == 'status':
        emit({'workers': queue.workers(), 'jobs': queue.status()})
    elif args.command == 'config':
        emit(config)
    elif args.command == 'config-ui':
        return serve_config_ui(args.home, args.host, args.port)
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
        max_tokens = args.max_tokens or config['max_tokens']
        timeout = args.timeout or config['timeout']
        if timeout < 1 or max_tokens < 1:
            raise ValueError('Timeout and token budget must be positive')
        # One runner across all queue directories protects the physical worker caps.
        with runner_lock(DEFAULT_HOME / 'runner.lock'):
            queue.recover()
            configured = enabled_workers(config)
            return drain(queue, args.home, args.slots, args.watch, model=args.model or config['model'],
                         max_tokens=max_tokens, timeout=timeout,
                         reasoning=args.reasoning, workers=configured)
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        emit({'error': str(exc)}); raise SystemExit(1)
