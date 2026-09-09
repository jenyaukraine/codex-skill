# Dispatcher: canonical usage

From any working directory:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" add --file "C:/absolute/path/tasks.json"
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" status
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" summary --prefix project-20260909-
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" health
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" warmup
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" result project-20260909-example
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" config
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" config-ui
```

`add` atomically enqueues the manifest and automatically starts one hidden persistent runner, or reuses the active one. No second launch command is required. The runner waits for new work when idle and fills any available slot (three per host). It sends requests to already running, healthy LM Studio APIs; it does not launch LM Studio, load models on purpose, or start Codex.

Manifest (UTF-8 JSON):

```json
[
  {"id":"project-20260909-example","prompt":"One exact task with relevant source, interfaces and acceptance criteria"},
  {"id":"project-20260909-example-2","prompt":"An independent follow-up task"}
]
```

Use no worker field. Old manifests may contain worker ids as strings under `worker`, but these hints do not constrain routing. Unknown fields, invalid types and duplicate IDs are rejected. IDs remain reserved even after completion.

## Worker configuration

The live configuration is stored at `C:/Users/jenya/.codex/bionic-dispatcher/config.json`.

Defaults:

- `21`: `http://192.168.88.21:1234/v1`, `slots: 3`
- `33`: `http://192.168.88.33:1234/v1`, `slots: 3`

Open the local configuration page with `config-ui`. It allows adding local/LAN machines, editing base URLs, enabling/disabling workers, setting slots per worker, and overriding model, timeout, max output tokens, context window, and TTL per worker. Set slots to `0` or disable a worker to keep it out of dispatch. Existing watch runners should be restarted to apply changed slot counts. If the requested UI port is busy, `config-ui` binds a free local fallback port and prints it.

Default context window is `230000`; default TTL is `900` seconds. `warmup` sends a tiny request with the configured model, context window, and TTL to make the model hot before queue work. Normal generation requests also include TTL so LM Studio can unload the model after idle time. `max_tokens` is only the response/output budget and is clamped to at least `32768`.

## State and interpretation

Default persistent data: `C:/Users/jenya/.codex/bionic-dispatcher/queue.sqlite3`; log: `runner.log` in the same directory. Queue status is the source of truth for local dispatch, not evidence of GPU utilization.

- `queued`: ready to be sent.
- `running`: request issued, answer awaited.
- `done`: complete model response, still requires Codex review.
- `incomplete`: output budget exhausted or empty result; preserved, never retried automatically.
- `uncertain`: error/timeout/interruption; the host is paused because upstream work may continue.

`result ID` returns full model output without printing its submitted prompt. Model code never runs automatically. Codex must review, integrate and run checks. Generation is not a test run.

In persistent `--watch` mode, blocked enabled workers are probed periodically with `/models`. If the configured model is visible for several stable probes and the worker has no running job in the queue, the dispatcher automatically clears the block and the worker resumes claiming FIFO work. A single successful probe is intentionally not enough because LM Studio may still be launching or loading a model. The failed job remains `uncertain`; it is not retried under the same id.

## Recovery / maintenance only

Do not call these during ordinary task submission:

- `run --watch --slots 3`: foreground runner for diagnosing startup. The global OS lock prevents a second runner. Normal `add` starts this itself.
- `run` without `--watch`: drains a batch then exits. Do not use it for interactive work.
- `add --no-start`: insert only, for isolated test queues.
- `--home <directory>` before a command: isolated queue for tests; do not use alternate homes for live work while the main dispatcher runs.
- `unblock 21 --note "Operator confirmed generation stopped"`: after actual operator confirmation. User reports that a host is idle, restarted, or not generating are sufficient confirmation; do not ask again in that case. Does not retry uncertain jobs.

Never kill a worker or silently repeat timed-out work. If the runner process exits while requests are outstanding, the next start marks interrupted requests uncertain. Keep the default queue across sessions so those records remain visible. The background runner is reusable; its idle state consumes no model requests. If the user says the upstream worker is idle or was restarted, unblock the paused host directly.

`client.py` is an internal text transport. For read-only model discovery only: `python <skill>/scripts/client.py --worker 21 --models`. Do not send generation through it, raw HTTP, project helper scripts, or multiple queue processes.

Default model: qwen3.8-9b-distill; OpenAI-compatible chat transport; max output tokens 32768 minimum; context window 230000; TTL 900 seconds; timeout 180 seconds. Do not use qwen3.8-9b-coder for this queue; it returns LM Studio server errors. Pass `run --reasoning off` only for models verified with LM Studio's native chat endpoint. Live defaults are centralized in `config.json`, falling back to `worker_config.py` defaults. Prefer `summary` over full `status` when the queue is large. Tests: `python -m unittest test_dispatcher -v` from the skill scripts directory.
