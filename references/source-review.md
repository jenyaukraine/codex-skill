# Source review adapter

`scripts/source_review.py` is a portable adapter over `dispatcher.py add`, not a separate runner. It uses the installed dispatcher's live worker settings and shared queue. It requires only Python's standard library.

Prepare and submit up to six TSX/CSS file reviews (or select files explicitly):

```powershell
python "<skill>/scripts/source_review.py" --workspace "D:/project" --file components/button.tsx --file app/globals.css
```

Without `--workspace`, the current directory is the project. Without `--file`, discovery scans `app`, `pages`, `components`, `styles`, and `src`, skipping hidden directories, nested `node_modules`, and linked trees. `--limit N` caps tasks, not worker concurrency. Files larger than 60,000 bytes are skipped rather than truncated; review large modules through a focused manifest with the relevant excerpts and interfaces. Empty selections fail without submission.

Use `--prepare-only` to save manifests without queueing work. Each run prints its directory under `<project>/bionic_output/`:

- `tasks.json`: dispatcher-compatible IDs and prompts containing current source and SHA-256.
- `run.json`: project location, source paths/hashes, task mapping and oversized-file skip reasons.

IDs are deterministic for project, file, source hash and review instructions, including across process restarts. A duplicate ID causes the dispatcher's entire submission to fail atomically. Inspect the earlier run/results and select genuinely new work; do not add random IDs to evade reservations or retry uncertain work. Preparation and collection never claim acceptance.

Collect a snapshot of full responses by task ID:

```powershell
python "<skill>/scripts/source_review.py" --collect "D:/project/bionic_output/<run-directory>"
```

This writes `results.jsonl` with full result payloads, queue status, pending review status and `source_changed`. It can be repeated while tasks are queued/running; it does not wait, retry generation, apply code or archive jobs. Lookup failures remain visible in the report. An ID mismatch fails instead of attaching an answer to the wrong file. Archived results remain retrievable through the dispatcher.

Review every response against the current source, verify dependencies where needed, and run actual checks before accepting. The default prompt requests a bounded UI/accessibility review; custom refactors needing surrounding interfaces belong in an explicit dispatcher manifest. Output length is never evidence of correctness. Keep `bionic_output/` out of version control because it contains source and model output.

Isolated validation from `<skill>/scripts`: `python -m unittest test_source_review test_dispatcher -v`. Tests use temporary projects and a separate queue, with simulated completion; they do not send LAN generation requests.
