# Frontier Token Saving Mode

Use this mode when the user wants local Bionic workers to absorb as much routine work as possible while Codex keeps final responsibility.

## Operating model

Codex should spend frontier tokens on:

- reading the real repository state,
- defining boundaries and acceptance criteria,
- integrating accepted worker output,
- running tests and visual checks,
- deciding what is actually done.

Local workers should receive any text-only work that can be reviewed independently:

- route/page audits,
- SQL and schema review,
- edge-case lists,
- implementation sketches,
- test-case drafts,
- UI/CSS critique,
- migration mapping,
- acceptance checklists,
- documentation first drafts,
- risk registers,
- result synthesis by prefix.

## Queue policy

Keep a small ready backlog during active project work. Prefer 10-30 short tasks over a few huge prompts. A good task should fit in one screen, name the exact module, and demand concrete output.

Use `summary --prefix <prefix>` instead of full `status` for routine checks. Top up the queue when `queued` falls below roughly one full worker wave, unless unreviewed `done` results are the real bottleneck.

## Task sizing

For local models, split prompts by module and ask for:

- findings first,
- exact files/routes/entities when known,
- small patch sketches instead of complete rewrites,
- tests and acceptance criteria,
- explicit uncertainty when context is missing.

Avoid sending long source files unless the worker truly needs them. Summarize interfaces and constraints instead.

## Reliability

If a worker repeatedly times out, reduce its slots, lower per-worker max tokens, or increase its per-worker timeout in `config-ui`. Do not bypass the dispatcher or manually send generation requests.

`uncertain` means the host is paused because the upstream request might still be running. Only unblock after the operator confirms the upstream generation has stopped or the worker was restarted.
