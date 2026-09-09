# Bionic Local

Codex skill for using two local LM Studio/Bionic workers through one durable shared FIFO queue.

## What It Does

- Sends text-only generation tasks to approved local workers.
- Keeps one shared queue for both machines.
- Uses up to three requests per host, six total.
- Lets you configure worker URLs, enabled state, and slots per machine.
- Automatically starts or reuses the persistent dispatcher runner.
- Preserves uncertain results and blocks a host until the operator confirms the upstream generation stopped.
- Keeps Codex responsible for reviewing, integrating, testing, and accepting generated output.

## Current Worker Map

- `21`: `http://192.168.88.21:1234/v1`
- `5`: `http://192.168.88.5:1234/v1`
- `localhost:1234/v1` is the same physical machine as `21`, not a third worker.

Default model: `qwen3.8-9b-distill`.

## Usage

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

Check status:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" status
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
- changing default model, timeout, and token budget.

Read a result:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" result project-20260909-example
```

Unblock a worker only after the operator confirms the upstream generation has stopped:

```powershell
python "C:/Users/jenya/.codex/skills/bionic-local/scripts/dispatcher.py" unblock 5 --note "Operator confirmed generation stopped"
```

## Rights

This repository is owned by Jenya Ukraine. See [LICENSE](LICENSE).

No permission is granted to copy, publish, sublicense, sell, or reuse this skill outside the owner's personal Codex setup unless the owner gives explicit written permission.
