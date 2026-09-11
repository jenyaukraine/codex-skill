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
from worker_config import DEFAULT_CONTEXT_WINDOW, DEFAULT_MODEL, DEFAULT_TTL_SECONDS, MIN_MAX_TOKENS, default_config, enabled_workers, load_config, save_config

DEFAULT_HOME = Path(__file__).resolve().parents[3] / 'bionic-dispatcher'
AUTO_RESUME_INTERVAL_SECONDS = 20
AUTO_RESUME_HEALTH_TIMEOUT_SECONDS = 5
AUTO_RESUME_REQUIRED_STABLE_PROBES = 3


def validate_tasks(tasks, worker_ids=None):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Expected a nonempty list of tasks')
    worker_ids = set(worker_ids or ('21', '33'))
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


def execute_job(job, worker, directory, model, max_tokens, timeout, reasoning, base_url=None, ttl=DEFAULT_TTL_SECONDS, context_window=DEFAULT_CONTEXT_WINDOW):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', dir=directory, delete=False) as prompt:
        prompt.write(job['prompt'])
        prompt_path = Path(prompt.name)
    try:
        command = [sys.executable, str(Path(__file__).with_name('client.py')), '--via-dispatcher',
                   '--prompt-file', str(prompt_path), '--model', model, '--max-tokens', str(max_tokens),
                   '--timeout', str(timeout), '--ttl', str(ttl), '--context-window', str(context_window)]
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


def should_block_worker(status, result):
    if status != 'uncertain':
        return False
    text = json.dumps(result or {}, ensure_ascii=False).lower()
    unavailable_markers = (
        'actively refused',
        'connection refused',
        'no connection could be made',
        'failed to establish a new connection',
        'max retries exceeded',
        'local api or input file unavailable',
        'target machine actively refused',
        'loaded model unavailable',
    )
    return any(marker in text for marker in unavailable_markers)


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


def drain(queue, directory, slots=3, watch=False, execute=execute_job, model=DEFAULT_MODEL, max_tokens=MIN_MAX_TOKENS, timeout=900, ttl=DEFAULT_TTL_SECONDS, context_window=DEFAULT_CONTEXT_WINDOW, reasoning='openai', workers=None):
    if workers is None:
        workers = [{'id': '21', 'base_url': None, 'slots': slots}, {'id': '33', 'base_url': None, 'slots': slots}]
    stop = threading.Event()
    output_lock = threading.Lock()
    probe_lock = threading.Lock()
    last_probe = {}
    healthy_probe_streak = {}

    def maybe_auto_resume(worker):
        if not watch:
            return
        worker_id = worker['id']
        now = time.monotonic()
        with probe_lock:
            if now - last_probe.get(worker_id, 0) < AUTO_RESUME_INTERVAL_SECONDS:
                return
            last_probe[worker_id] = now
        try:
            resumed = auto_resume_worker(queue, worker, model, healthy_probe_streak)
        except Exception as exc:
            with output_lock:
                emit({'event': 'auto_resume_probe_failed', 'worker': worker_id, 'error': str(exc)[:1000]})
            return
        if resumed:
            with output_lock:
                emit({'event': 'auto_resumed', 'worker': worker_id})

    def slot(worker):
        worker_id = worker['id']
        while not stop.is_set():
            maybe_auto_resume(worker)
            job = queue.claim(worker_id)
            if job is None:
                if watch:
                    stop.wait(.5)
                    continue
                return
            with output_lock:
                emit({'event': 'started', 'id': job['id'], 'worker': worker_id})
            try:
                worker_model = worker.get('model') or model
                worker_max_tokens = worker.get('max_tokens') or max_tokens
                if worker_max_tokens < MIN_MAX_TOKENS:
                    worker_max_tokens = MIN_MAX_TOKENS
                worker_timeout = worker.get('timeout') or timeout
                worker_ttl = worker.get('ttl') or ttl
                worker_context = worker.get('context_window') or context_window
                status, result = execute(
                    job, worker_id, directory, worker_model, worker_max_tokens,
                    worker_timeout, reasoning, worker.get('base_url'), worker_ttl, worker_context
                )
            except Exception as exc:
                status, result = 'uncertain', {'error': str(exc)[:1000]}
            queue.finish(job['id'], status, result, block_worker=should_block_worker(status, result))
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


def status_summary(queue, prefix='', limit=12):
    workers = queue.workers()
    jobs = queue.status()
    if prefix:
        jobs = [job for job in jobs if job['id'].startswith(prefix)]
    counts = {}
    for job in jobs:
        counts[job['status']] = counts.get(job['status'], 0) + 1
    return {
        'workers': workers,
        'counts': counts,
        'running': [job for job in jobs if job['status'] == 'running'][:limit],
        'queued_head': [job for job in jobs if job['status'] == 'queued'][:limit],
        'blocked_workers': [worker for worker in workers if worker['blocked']],
        'prefix': prefix,
    }


def worker_health(workers, timeout=10):
    results = []
    for worker in workers:
        started = time.monotonic()
        command = [
            sys.executable,
            str(Path(__file__).with_name('client.py')),
            '--base-url',
            worker['base_url'],
            '--models',
            '--timeout',
            str(timeout),
        ]
        latency_ms = round((time.monotonic() - started) * 1000)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=timeout + 5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            results.append({
                'id': worker['id'],
                'name': worker['name'],
                'base_url': worker['base_url'],
                'enabled': worker['enabled'],
                'slots': worker['slots'],
                'ok': False,
                'latency_ms': latency_ms,
                'models': [],
                'error': str(exc)[:1000],
            })
            continue
        latency_ms = round((time.monotonic() - started) * 1000)
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            payload = {}
        results.append({
            'id': worker['id'],
            'name': worker['name'],
            'base_url': worker['base_url'],
            'enabled': worker['enabled'],
            'slots': worker['slots'],
            'ok': result.returncode == 0,
            'latency_ms': latency_ms,
            'models': payload.get('models', []),
            'error': '' if result.returncode == 0 else (result.stderr.strip() or result.stdout.strip())[:1000],
        })
    return results


def worker_warmup(workers, model, context_window, ttl, timeout=60):
    results = []
    for worker in workers:
        worker_model = worker.get('model') or model
        worker_context = worker.get('context_window') or context_window
        worker_ttl = worker.get('ttl') or ttl
        worker_timeout = worker.get('timeout') or timeout
        command = [
            sys.executable,
            str(Path(__file__).with_name('client.py')),
            '--base-url',
            worker['base_url'],
            '--warmup',
            '--model',
            worker_model,
            '--context-window',
            str(worker_context),
            '--ttl',
            str(worker_ttl),
            '--timeout',
            str(worker_timeout),
        ]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=worker_timeout + 5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            results.append({
                'id': worker['id'],
                'base_url': worker['base_url'],
                'ok': False,
                'latency_ms': round((time.monotonic() - started) * 1000),
                'error': str(exc)[:1000],
            })
            continue
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            payload = {}
        results.append({
            'id': worker['id'],
            'base_url': worker['base_url'],
            'ok': result.returncode == 0 and payload.get('complete') is True,
            'latency_ms': round((time.monotonic() - started) * 1000),
            'model': payload.get('model', worker_model),
            'context_window_requested': worker_context,
            'ttl': worker_ttl,
            'content': payload.get('content', ''),
            'error': '' if result.returncode == 0 else (result.stderr.strip() or result.stdout.strip())[:1000],
        })
    return results


def auto_resume_worker(queue, worker, default_model, healthy_probe_streak=None, timeout=AUTO_RESUME_HEALTH_TIMEOUT_SECONDS, required_stable_probes=AUTO_RESUME_REQUIRED_STABLE_PROBES):
    if not queue.worker_blocked(worker['id']):
        if healthy_probe_streak is not None:
            healthy_probe_streak.pop(worker['id'], None)
        return False
    result = worker_health([worker], timeout=timeout)[0]
    required_model = worker.get('model') or default_model
    if not result.get('ok') or required_model not in result.get('models', []):
        if healthy_probe_streak is not None:
            healthy_probe_streak[worker['id']] = 0
        reason = result.get('error') or f"model {required_model} not visible"
        queue.update_worker_note(worker['id'], f"Waiting for stable /models before auto-resume: {reason[:800]}")
        return False
    if healthy_probe_streak is not None:
        healthy_probe_streak[worker['id']] = healthy_probe_streak.get(worker['id'], 0) + 1
        if healthy_probe_streak[worker['id']] < required_stable_probes:
            queue.update_worker_note(
                worker['id'],
                f"Waiting for stable /models before auto-resume: {healthy_probe_streak[worker['id']]}/{required_stable_probes} ok probes for {required_model}",
            )
            return False
    return queue.unblock_if_idle(
        worker['id'],
        f"Auto-resumed: /models stable and {required_model} available",
    )


def render_config_page(config, message=''):
    worker_rows = []
    for index, worker in enumerate(config['workers']):
        checked = 'checked' if worker['enabled'] else ''
        worker_rows.append(f"""
        <tr>
          <td><input name="id_{index}" value="{escape(worker['id'])}" required></td>
          <td><input name="name_{index}" value="{escape(worker['name'])}" required></td>
          <td><input name="base_url_{index}" value="{escape(worker['base_url'])}" required></td>
          <td><input type="number" min="0" max="12" name="slots_{index}" value="{worker['slots']}" required></td>
          <td><input name="model_{index}" value="{escape(worker.get('model', ''))}" placeholder="default"></td>
          <td><input type="number" min="0" name="max_tokens_{index}" value="{worker.get('max_tokens', 0)}"></td>
          <td><input type="number" min="0" name="context_window_{index}" value="{worker.get('context_window', 0)}"></td>
          <td><input type="number" min="0" name="ttl_{index}" value="{worker.get('ttl', 0)}"></td>
          <td><input type="number" min="0" name="timeout_{index}" value="{worker.get('timeout', 0)}"></td>
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
    <div><label>Max output tokens</label><input type="number" min="{MIN_MAX_TOKENS}" name="max_tokens" value="{config['max_tokens']}" required></div>
    <div><label>Context window</label><input type="number" min="1" name="context_window" value="{config.get('context_window', DEFAULT_CONTEXT_WINDOW)}" required></div>
    <div><label>TTL seconds</label><input type="number" min="1" name="ttl" value="{config.get('ttl', DEFAULT_TTL_SECONDS)}" required></div>
    <div><label>Timeout seconds</label><input type="number" min="1" name="timeout" value="{config['timeout']}" required></div>
  </div>
  <table>
    <thead><tr><th>ID</th><th>Name</th><th>Base URL</th><th>Slots</th><th>Model</th><th>Output</th><th>Ctx</th><th>TTL</th><th>Timeout</th><th>Enabled</th></tr></thead>
    <tbody>{''.join(worker_rows)}
      <tr><td><input name="id_new" placeholder="new-id"></td><td><input name="name_new" placeholder="New worker"></td><td><input name="base_url_new" placeholder="http://192.168.88.50:1234/v1"></td><td><input type="number" min="0" max="12" name="slots_new" value="1"></td><td><input name="model_new" placeholder="default"></td><td><input type="number" min="0" name="max_tokens_new" value="0"></td><td><input type="number" min="0" name="context_window_new" value="0"></td><td><input type="number" min="0" name="ttl_new" value="0"></td><td><input type="number" min="0" name="timeout_new" value="0"></td><td><input type="checkbox" name="enabled_new"></td></tr>
    </tbody>
  </table>
  <div class="actions"><button>Save configuration</button><a class="button secondary" href="/">Reload</a></div>
  <p class="hint">Output tokens are clamped to at least {MIN_MAX_TOKENS}. Context window defaults to {DEFAULT_CONTEXT_WINDOW}. TTL keeps a warmed model loaded after work, then lets LM Studio unload it when idle. Set slots to 0 or disable a flaky machine.</p>
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
            'model': form.get('model_' + index, [''])[0],
            'max_tokens': int(form.get('max_tokens_' + index, ['0'])[0] or 0),
            'context_window': int(form.get('context_window_' + index, ['0'])[0] or 0),
            'ttl': int(form.get('ttl_' + index, ['0'])[0] or 0),
            'timeout': int(form.get('timeout_' + index, ['0'])[0] or 0),
            'enabled': ('enabled_' + index) in form,
        })
    return {
        'model': form.get('model', [''])[0],
        'max_tokens': int(form.get('max_tokens', [str(MIN_MAX_TOKENS)])[0] or MIN_MAX_TOKENS),
        'context_window': int(form.get('context_window', [str(DEFAULT_CONTEXT_WINDOW)])[0] or DEFAULT_CONTEXT_WINDOW),
        'ttl': int(form.get('ttl', [str(DEFAULT_TTL_SECONDS)])[0] or DEFAULT_TTL_SECONDS),
        'timeout': int(form.get('timeout', ['900'])[0] or 900),
        'workers': workers,
    }


def serve_config_ui(home, host, port, open_browser=True):
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

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        server = ThreadingHTTPServer((host, 0), Handler)
    url = f'http://{host}:{server.server_port}/'
    print(json.dumps({'config_ui': url}, ensure_ascii=False), flush=True)
    if open_browser:
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
    archive = commands.add_parser('archive', help='Archive explicitly reviewed terminal jobs from an acceptance manifest')
    archive.add_argument('--file', type=Path, required=True)
    run = commands.add_parser('run', help='Fill each free slot immediately from the queue')
    run.add_argument('--slots', type=int, choices=range(1, 5), default=3)
    run.add_argument('--watch', action='store_true', help='Wait for new tasks until Ctrl+C')
    run.add_argument('--model')
    run.add_argument('--max-tokens', type=int)
    run.add_argument('--timeout', type=int)
    run.add_argument('--reasoning', choices=('openai', 'off', 'on'), default='openai')
    commands.add_parser('status')
    summary = commands.add_parser('summary')
    summary.add_argument('--prefix', default='')
    summary.add_argument('--limit', type=int, default=12)
    health = commands.add_parser('health')
    health.add_argument('--timeout', type=int, default=10)
    warmup = commands.add_parser('warmup', help='Warm enabled workers with the configured model, context window, and TTL')
    warmup.add_argument('--timeout', type=int, default=60)
    commands.add_parser('config')
    config_ui = commands.add_parser('config-ui')
    config_ui.add_argument('--host', default='127.0.0.1')
    config_ui.add_argument('--port', type=int, default=8795)
    config_ui.add_argument('--no-open', action='store_true')
    result = commands.add_parser('result'); result.add_argument('id')
    unblock = commands.add_parser('unblock'); unblock.add_argument('worker'); unblock.add_argument('--note', required=True)
    args = parser.parse_args()
    args.home.mkdir(parents=True, exist_ok=True)
    config = load_config(args.home)
    queue = Queue(args.home / 'queue.sqlite3')
    if args.command != 'archive':
        queue.sync_workers(config['workers'])
    if args.command == 'add':
        tasks = validate_tasks(json.loads(args.file.read_text(encoding='utf-8-sig')), {worker['id'] for worker in config['workers']})
        queue.add(tasks); emit({'added': len(tasks), 'ids': [t['id'] for t in tasks]})
        if not args.no_start:
            emit({'runner': ensure_runner(args.home)})
    elif args.command == 'archive':
        entries = json.loads(args.file.read_text(encoding='utf-8-sig'))
        emit(queue.archive(entries))
    elif args.command == 'status':
        emit({'workers': queue.workers(), 'jobs': queue.status()})
    elif args.command == 'summary':
        emit(status_summary(queue, args.prefix, args.limit))
    elif args.command == 'health':
        emit({'workers': worker_health(config['workers'], args.timeout)})
    elif args.command == 'warmup':
        emit({'workers': worker_warmup(enabled_workers(config), config['model'], config['context_window'], config['ttl'], args.timeout)})
    elif args.command == 'config':
        emit(config)
    elif args.command == 'config-ui':
        return serve_config_ui(args.home, args.host, args.port, not args.no_open)
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
        if timeout < 1:
            raise ValueError('Timeout must be positive')
        if max_tokens < MIN_MAX_TOKENS:
            max_tokens = MIN_MAX_TOKENS
        # One runner across all queue directories protects the physical worker caps.
        with runner_lock(DEFAULT_HOME / 'runner.lock'):
            queue.recover()
            configured = enabled_workers(config)
            return drain(queue, args.home, args.slots, args.watch, model=args.model or config['model'],
                         max_tokens=max_tokens, timeout=timeout, ttl=config['ttl'],
                         context_window=config['context_window'],
                         reasoning=args.reasoning, workers=configured)
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        emit({'error': str(exc)}); raise SystemExit(1)
