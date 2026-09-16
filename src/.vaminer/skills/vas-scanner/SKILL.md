---
name: vas-scanner
description: Run a bundled VAS rule against a repository with ast-grep discovery and parallel Agent task analysis.
---

# VAS Scanner

Use this skill to scan one repository with one bundled `VAS-<digits>` rule. The
Python CLI performs four mechanical actions: preflight, Anchor discovery and
task preparation, result recording, and final report aggregation. The main
Agent owns overview creation, task scheduling, retries, and subagent analysis.

## Requirements

- Python 3.12 or newer with its standard library.
- Native `ast-grep` or `sg` available on `PATH`.
- The complete skill directory, including `scripts/`, `rules/`, and this file.
- A readable target repository with a writable `.vas` directory.

The scanner does not require VAMiner, `portalocker`, `ast-grep-py`, or any
other Python package.

## Run protocol

1. Run preflight before doing repository work:

   ```bash
   python3 <skill-dir>/scripts/scan.py preflight <VAS-ID> <repo-path>
   ```

   Stop on failure and tell the user which dependency or path must be fixed.

2. Ensure `<repo>/.vas/repository_overview.md` exists. Reuse it by default. If
   the user requests refresh, inspect the README first, then the directory
   layout and representative source files, and write a concise overview with
   `Code Layout`, `Module Relations`, and `Key Interfaces`.

3. Prepare the run:

   ```bash
   python3 <skill-dir>/scripts/scan.py prepare <VAS-ID> <repo-path>
   ```

   Read the returned configuration and task list. Preparation creates an
   immutable `manifest.json`, a run-local overview snapshot, one Markdown task
   per admitted candidate, and empty `shared_checks/` and `results/`
   directories. The manifest reports the configured concurrency and task count.

4. Schedule tasks in task ID order with at most the manifest concurrency. Start
   one fresh subagent per task. Give each subagent only its task Markdown path.
   When a subagent fails, times out, or leaves no result, retry that task before
   finalization. A task is complete when `results/TASK-xxxx.json` exists.

5. After every task has recorded successfully, finalize once:

   ```bash
   python3 <skill-dir>/scripts/scan.py finalize <run-dir>
   ```

   A nonzero result identifies missing, extra, or invalid task results. Retry
   the named tasks and call finalize again. A successful call writes the final
   `report.json` and returns its path and report count.

## Run layout

```text
<repo>/.vas/<VAS-ID>/run_<timestamp>/
├── manifest.json
├── repository_overview.md
├── tasks/TASK-0001.md
├── shared_checks/<repository-relative-file>.md
├── results/TASK-0001.json
└── report.json
```

`manifest.json` is the immutable task identity and configuration map. It has
the rule ID, repository, overview snapshot name, concurrency settings, task
count, and `task_id` to candidate-file mappings. It has no subagent status.

`results/TASK-xxxx.json` contains that task's `reports` array. A task with no
defect writes `[]`. `shared_checks/` contains the task's Anchor Facts as
navigation context for other agents; missing shared context calls for fresh
source inspection.

## Subagent contract

The task Markdown is the complete analysis handoff. Read the overview first,
inspect every listed Anchor location in the primary candidate, and trace related
files when needed. Before reading a related file, read its mirrored Markdown in
`shared_checks/` when present and decide whether the Fact applies.

Return exactly one object with `anchorFacts` and `reports`, then call:

```bash
python3 <skill-dir>/scripts/scan.py record <run-dir> <task-id> <<'JSON'
<the JSON object>
JSON
```

`record` checks only the JSON shape and basic field types. It writes Facts to
the candidate's shared-check Markdown and writes reports to the task result
file. If it reports a schema error, correct the object and retry. End only
after record succeeds.

The task template is defined in `scripts/prompt.py`; update that template when
the subagent contract changes.
