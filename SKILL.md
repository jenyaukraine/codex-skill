---
name: bionic-local
description: Use the user's two local Bionic/LM Studio workers through a persistent shared queue, with three requests per host and Codex verification. Use when the user requests local workers, Bionic, or this skill; does not control the Bionic desktop chat.
---

# Bionic local workers

## One supported launch path

Use `scripts/dispatcher.py add --file <absolute-manifest.json>` for **all generation tasks**, including one task. It automatically starts the persistent background runner if needed. Do not manually start another runner, call the LAN generation API, run `client.py --prompt-file`, or use project `run-500-tasks.ps1` / `.backup/**/run.py` launchers. They bypass the shared scheduler. `client.py` is an internal transport; only `--models` is a supported diagnostic command.

Read [dispatcher usage](references/dispatcher.md) before first use. The paths are relative to this skill. The dispatcher is installed outside projects and works from any working directory.

When the user asks to save frontier-model tokens, let local workers cover more routine work, or run "local-worker-first", also read [frontier token saving mode](references/token-saving.md).

## Current user preferences (2026-09-09)

The host and slot values below record the original defaults. Read live `config` for the current enabled hosts, slots and model overrides; do not reset a later user configuration to these defaults.

- Hosts: `21` = `http://192.168.88.21:1234/v1`; `33` = `http://192.168.88.33:1234/v1`. Localhost is the same physical machine as 21, never a third worker.
- Three requests per host, six total by default. Hardware supports four but three is the current requested limit. Per-worker slots and URLs can be changed through the local config UI.
- One shared FIFO queue. Any available host takes the next task; do not pin tasks to a host. Legacy `worker` fields are accepted but ignored for routing.
- Prepare a useful backlog before dispatch, then enqueue ready follow-ups as results arrive. Do not wait for all six results to start preparing the next iteration. If results awaiting review accumulate, finish acceptance instead of generating busywork.
- Default model `qwen3.8-9b-distill`, OpenAI-compatible chat transport, no native per-request reasoning flag. Verified on host 33. Do not use `qwen3.8-9b-coder` for this queue because it returns LM Studio server errors. Do not change desktop model settings. A task ID is unique across the saved queue; use project, date and a meaningful suffix.

User preference: proactively delegate useful independent parts of the current authorized task to these workers, including implementation and test drafts. Do not reserve them only for trivial microtasks. Codex keeps integration and actual execution/acceptance.

Aggressive utilization preference: keep the shared queue warm whenever there is any useful independent work and the workers are already healthy. If a task can be split into reviewable text-only units such as edge-case audit, route check, CSS critique, SQL query review, test-case draft, migration risk, copy pass, acceptance checklist, or implementation sketch, enqueue it instead of leaving healthy workers idle. Prefer small bounded tasks with concrete acceptance criteria over waiting for a large perfect batch. Top up the queue opportunistically while Codex continues integration work, unless enough `done` results are already awaiting review that acceptance work is clearly the bottleneck. Do not create pressure on a machine whose local API is starting, loading a model, refusing connections, or missing the configured model.

Always-warm queue preference: when the user explicitly wants the local workers heavily used, maintain a backlog of 12-18 small ready tasks whenever there is useful independent work left, acceptance is keeping up, and the local APIs are healthy. Treat running tasks with no queued head as underfilled only for healthy, unblocked workers with review capacity. Before refilling, check `summary` and the acceptance record: deduplicate proposed tasks against queued/running work and reviewed findings by module, issue and source revision, not just task ID. If the queue is below the requested target and those conditions hold, enqueue another short manifest instead of waiting for a perfect batch. If a host is blocked because a request failed while the model was already loaded and the user confirms generation stopped, unblock it when the API is healthy; refill only if useful work and review capacity remain. If the host is launching, loading, refusing connections, or missing the configured model, wait for stable `/models` health instead of unblocking or sending generation. Use focused microtasks that can finish quickly and be reviewed independently.

## Integration and release mode

When the user asks to collect worker results, integrate changes or release, prioritize acceptance over utilization. Stop submitting broad audit, brainstorming or checklist batches while relevant results await review. Idle workers are acceptable when acceptance is the bottleneck; the heavy-use preferences above do not override this. Leave existing queue/running jobs intact unless changing them is separately authorized. Submit a focused follow-up only when it resolves a concrete integration blocker that existing results do not cover.

Track review in the project's existing work notes or a compact acceptance record: task ID, supplied source revision/hash, accepted change and affected files, Codex-run checks and outcomes, or rejection/duplicate/stale reason. Several IDs may map to one verified change. Queue `done` counts are not accepted-change counts. Before applying an older result, compare its assumptions with current source; adapt and verify valid findings, and reject obsolete or unsupported ones. Use this record to account for results and produce release notes from actual accepted changes.

If the user asks to remove processed entries, archive only explicitly reviewed terminal IDs with `dispatcher.py archive --file <acceptance-manifest.json>` after acceptance. Full prompts/results remain recoverable and IDs stay reserved; queued/running work cannot be archived. Read the archive manifest and transaction rules in [dispatcher usage](references/dispatcher.md). Do not clear the queue merely because results are `done`.

Frontier token saving preference: when the user wants lower frontier-token use, operate in local-worker-first mode. Codex keeps repo reading, edits, tests, browser verification, and final decisions, while workers draft audits, specs, test cases, risk registers, implementation sketches, and acceptance checklists. Keep prompts short and module-scoped so local models finish reliably.

## Responsibilities and boundaries

Workers receive text only. They cannot read files, run tests, edit the project, start processes, or use tools. For code work, supply the current relevant source excerpts and exact interfaces, paths, a base revision and/or content hash (include a content hash for uncommitted source), edge cases, and a concrete expected patch, finding or test draft with acceptance criteria. Put this context inside `prompt`; manifest fields remain unchanged. A path or generic request to inspect a module is not source context. Ask workers to identify missing context instead of inventing repository behavior. Codex owns planning, integration, browser verification and test execution. Worker test commands and expected results are proposals; any claim that a worker ran tests is unsupported. Model response `done` is not acceptance or evidence of passed tests.

Review every artifact before executing or applying it. Preserve uncommitted changes. For a localized mistake, one specific correction request is reasonable; repeated failure should be finished in Codex. Do not replicate implementation inside tests merely to make a generated test runnable.

Verify external API signatures, tuple return positions and callback calling conventions against the actual dependency or authoritative documentation before accepting a patch. A worker's fixture/mock is not independent evidence of that contract. For callback or event-routing changes, exercise the real registration/dispatch path in a focused test; directly calling the final handler can miss argument-shifting and dropped-event defects.

The two LAN hosts are user-authorized for task context, not unrelated personal data or credentials. The dispatcher does not start LM Studio, install/load models, change authentication or fall back to cloud services. Report actual unavailability.

An interrupted runner recovery pauses affected hosts because upstream status is unknown. Ordinary job-level `uncertain` errors/timeouts release their slots without blocking the host, except when `should_block_worker` detects connection/API-unavailable markers in the error. Check `summary` for actual blocked-worker state; `uncertain` alone does not imply a paused host. Never automatically retry an uncertain job as the same id. In persistent watch mode, blocked enabled workers are probed through `/models` and automatically unblocked only after the configured model is visible for several stable probes and no job is still running on that worker. Treat user statements such as "простаивает", "перезапустил", "генерация не идет", or equivalent idle/restarted reports as sufficient operator confirmation only when the API is already healthy; while a model is launching/loading, keep the worker blocked and wait for stable `/models` before sending generation. Completed/incomplete/uncertain job results release their slots automatically. An empty queue is idle, not a scheduler error: state this accurately and do not claim GPU activity from local queue state alone.

Useful diagnostics: dispatcher `summary`, `health`, `status`, `result <id>`, and client `--worker 21 --models` / `--worker 33 --models`. Prefer `summary --prefix <task-prefix>` when the queue is large. Use `health` for `/models` latency and availability checks without sending generation prompts. See the reference for recovery and isolated tests. No direct generation commands should appear in handoffs.

Configuration: use `python "<skill>/scripts/dispatcher.py" config` for JSON config, `python "<skill>/scripts/dispatcher.py" config-ui` to open the local configuration page, or `python "<skill>/scripts/dispatcher.py" warmup` to pre-load enabled workers with the configured model/context/TTL. The UI supports default worker URLs, adding machines, enabling/disabling workers, per-worker slots, and per-worker model/output-token/context-window/TTL/timeout overrides. Context window defaults to `230000`; TTL defaults to `900` seconds. Output tokens are always at least `32768`. For a flaky host, lower its slots or raise timeout in config instead of bypassing the dispatcher.
