# Bionic Local

Codex skill for using two local LM Studio/Bionic workers through one durable shared FIFO queue.

## What It Does

- Sends text-only generation tasks to approved local workers.
- Keeps one shared queue for both machines.
- Uses up to three requests per host, six total.
- Lets you configure worker URLs, enabled state, and slots per machine.
- Automatically starts or reuses the persistent dispatcher runner.
- Preserves uncertain results; connection/API failures and interrupted-runner recovery can block a host. Watch mode resumes it after stable model health checks and no running jobs.
- Archives explicitly reviewed terminal results atomically, preserves their full contents, and keeps their IDs reserved.
- Keeps Codex responsible for reviewing, integrating, testing, and accepting generated output.
- Supports a local-worker-first workflow so small audits, specs, test drafts, and checklists use local machines before frontier-model tokens.

## Current Worker Map

- `21`: `http://192.168.88.21:1234/v1`
- `33`: `http://192.168.88.33:1234/v1`
- `localhost:1234/v1` is the same physical machine as `21`, not a third worker.

Default model: `qwen3.8-9b-distill-uncensored-heretic`.
Default context window for planning/diagnostics: `230000`.
Minimum max output tokens: `32768`.
Default TTL: `900` seconds.

## Usage

For TSX/CSS source reviews, the bundled `scripts/source_review.py --workspace <project>` helper prepares source-bearing tasks, submits them through the shared dispatcher and collects full results. See [source review usage](references/source-review.md) for file selection, preparation-only mode and collection.

Create a UTF-8 JSON manifest:

```json
[
  {
    "id": "project-20260909-example",
    "prompt": "One bounded task with context, required output, and acceptance criteria."
  }
]
```

Add it to the queue:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" add --file "C:/absolute/path/tasks.json"
```

For project work, prefer many short module-scoped tasks over one huge prompt. Codex should integrate and verify; workers should draft reviewable text artifacts.

Check status:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" status
```

Compact status for day-to-day use:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" summary --prefix bionic-skill-polish-
```

Check worker health without sending a generation prompt:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" health
```

Warm enabled workers with the configured model, context window, and TTL:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" warmup
```

Show JSON configuration:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" config
```

Open the local configuration window:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" config-ui
```

The configuration page supports:

- adding more LAN or loopback machines,
- changing base URLs,
- enabling or disabling a worker,
- setting slots per worker,
- changing default model, timeout, max output tokens, context window, and TTL,
- overriding model, timeout, max output tokens, context window, and TTL per worker.

The dispatcher checks the exact loaded model before generation and warmup. It preserves the loaded context window; the legacy context setting is not sent as a loading instruction. Requests still include `ttl`. `max_tokens` means output budget only and is clamped to at least `32768`.

If the default port is busy, `config-ui` automatically falls back to a free local port and prints the URL.

Read a result:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" result project-20260909-example
```

For manual recovery, unblock a worker after the operator confirms the upstream generation has stopped and its API is healthy:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" unblock 33 --note "Operator confirmed generation stopped"
```

To remove reviewed results from the active queue, use `archive --file <acceptance.json>`. See [the archive manifest and review requirements](references/dispatcher.md#archive-reviewed-results). Archived results remain available through `result ID`.

## Rights

This repository is owned by Jenya Ukraine. See [LICENSE](LICENSE).

No permission is granted to copy, publish, sublicense, sell, or reuse this skill outside the owner's personal Codex setup unless the owner gives explicit written permission.
