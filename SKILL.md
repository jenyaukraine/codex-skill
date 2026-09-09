---
name: bionic-local
description: Use the user's two local Bionic/LM Studio workers through a persistent shared queue, with three requests per host and Codex verification. Use when the user requests local workers, Bionic, or this skill; does not control the Bionic desktop chat.
---

# Bionic local workers

## One supported launch path

Use `scripts/dispatcher.py add --file <absolute-manifest.json>` for **all generation tasks**, including one task. It automatically starts the persistent background runner if needed. Do not manually start another runner, call the LAN generation API, run `client.py --prompt-file`, or use project `run-500-tasks.ps1` / `.backup/**/run.py` launchers. They bypass the shared scheduler. `client.py` is an internal transport; only `--models` is a supported diagnostic command.

Read [dispatcher usage](references/dispatcher.md) before first use. The paths are relative to this skill. The dispatcher is installed outside projects and works from any working directory.

## Current user preferences (2026-09-09)

- Hosts: `21` = `http://192.168.88.21:1234/v1`; `5` = `http://192.168.88.5:1234/v1`. Localhost is the same physical machine as 21, never a third worker.
- Three requests per host, six total. Hardware supports four but three is the current requested limit.
- One shared FIFO queue. Any available host takes the next task; do not pin tasks to a host. Legacy `worker` fields are accepted but ignored for routing.
- Prepare a useful backlog before dispatch, then enqueue ready follow-ups as results arrive. Do not wait for all six results to start preparing the next iteration. If results awaiting review accumulate, finish acceptance instead of generating busywork.
- Default model `qwen3.8-9b-distill`, OpenAI-compatible chat transport, no native per-request reasoning flag. Verified on host 5. Do not use `qwen3.8-9b-coder` for this queue because it returns LM Studio server errors. Do not change desktop model settings. A task ID is unique across the saved queue; use project, date and a meaningful suffix.

User preference: proactively delegate useful independent parts of the current authorized task to these workers, including implementation and test drafts. Do not reserve them only for trivial microtasks. Codex keeps integration and actual execution/acceptance.

Aggressive utilization preference: keep the shared queue warm whenever there is any useful independent work, even a micro-opportunity. If a task can be split into reviewable text-only units such as edge-case audit, route check, CSS critique, SQL query review, test-case draft, migration risk, copy pass, acceptance checklist, or implementation sketch, enqueue it instead of leaving workers idle. Prefer small bounded tasks with concrete acceptance criteria over waiting for a large perfect batch. Top up the queue opportunistically while Codex continues integration work, unless enough `done` results are already awaiting review that acceptance work is clearly the bottleneck.

## Responsibilities and boundaries

Workers receive text only. They cannot read files, run tests, edit the project, start processes, or use tools. Give each one a bounded execution assignment with relevant source, exact interfaces, edge cases and expected output. Codex owns planning, integration, browser verification and test execution. Model response `done` is not acceptance or evidence of passed tests.

Review every artifact before executing or applying it. Preserve uncommitted changes. For a localized mistake, one specific correction request is reasonable; repeated failure should be finished in Codex. Do not replicate implementation inside tests merely to make a generated test runnable.

The two LAN hosts are user-authorized for task context, not unrelated personal data or credentials. The dispatcher does not start LM Studio, install/load models, change authentication or fall back to cloud services. Report actual unavailability.

An uncertain response or timeout pauses that host because generation may still be running. Never automatically retry or unblock. Only release it after the operator confirms the upstream generation stopped. Completed/incomplete responses release their slots automatically. An empty queue is idle, not a scheduler error: state this accurately and do not claim GPU activity from local queue state alone.

Useful diagnostics: dispatcher `status`, `result <id>`, and client `--worker 21 --models` / `--worker 5 --models`. See the reference for recovery and isolated tests. No direct generation commands should appear in handoffs.
