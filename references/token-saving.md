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

Use `summary --prefix <prefix>` instead of full `status` for routine checks. Before refilling, deduplicate proposed work by module, issue and source revision against queued/running jobs and the acceptance record. Top up when `queued` falls below roughly one full worker wave only if review capacity remains. In integration/release mode, follow the acceptance-first rules in `SKILL.md`: stop broad new batches while relevant results await review, and use focused follow-ups only for concrete unresolved integration blockers. Heavy worker use does not require generating work faster than it can be accepted.

## Task sizing

For local models, split prompts by module and ask for:

- findings first,
- exact files/routes/entities when known,
- small patch sketches instead of complete rewrites,
- tests and acceptance criteria,
- explicit uncertainty when context is missing.

For code tasks, include current relevant source excerpts, paths and base revision/content hash; include a content hash for uncommitted source. Trim unrelated source and summarize surrounding interfaces and constraints, without substituting a generic module description for the actual code under review. Require a concrete patch, finding or test draft and explicit missing-context notes. Workers cannot execute tests: their commands and expected outcomes are proposals for Codex to verify.

## Reliability

If a worker repeatedly times out, reduce its slots or increase its per-worker timeout in `config-ui`. Do not bypass the dispatcher, lower output below the `32768` minimum, or manually send generation requests.

`uncertain` alone does not mean the host is paused: ordinary job errors/timeouts release slots; connection/API-unavailable markers and interrupted-runner recovery can block hosts. Check `summary` for actual blocked state. Treat direct user reports that a host is idle, restarted, or not generating as operator confirmation; unblock only when its API is already healthy, and otherwise wait for stable `/models` health. See `dispatcher.md` for recovery details.
