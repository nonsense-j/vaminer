---
name: vas-scanner
description: Scan a repository for defects defined by a bundled VAS rule. Use when asked to run a VAS-NNNN rule against a repository.
---

# VAS Scanner

Scan one repository with one bundled VAS rule. The required inputs are a `VAS-<digits>` rule ID and the target repository path. Use the current repository when the target is unambiguous; ask for the missing rule ID or repository only when it cannot be inferred.

The bundled scanner command is `python3 <skill-dir>/scripts/scan.py`. It validates the environment, creates candidate-analysis tasks, validates task results, and assembles the final report. The coordinating agent owns the general workflow management and task delegation. Each analysis subagent owns one generated task; the task Markdown is its complete contract.

## Scan Workflow

1. Run the scanner preflight:

   ```bash
   python3 <skill-dir>/scripts/scan.py preflight <VAS-ID> <repo-path>
   ```

   Continue only when preflight reports `ready`. On failure, report the feedback to user. The scanner uses `AST_GREP_CLI_PATH` from `scripts/config.py`; when it is `None`, preflight automatically installs a private CLI under `<skill-dir>/.tool/ast_grep`.

2. Check the repository overview:

   Ensure `<repo-path>/.vas/repository_overview.md` exists. Reuse an existing overview unless the user requests a refresh. If the overview is missing or needs to be refreshed, create it with an **explore subagent** to *inspect the repository README, source layout, and representative entry points, then summarize `Code Layout`, `Module Relations`, and `Key Interfaces`*. Keep the overview general and free of rule-specific conclusions.

3. Prepare the scan:

   ```bash
   python3 <skill-dir>/scripts/scan.py prepare <VAS-ID> <repo-path>
   ```

   Keep the returned `run_dir`, `tasks`, and `concurrency`. This will create tasks under `<run-dir>/tasks`, each targets a candidate file, ordered by their analysis priority. A task count of zero is valid and proceeds directly to finalization.

4. Delegate analysis tasks to subagents:

   Every created task should be delegated to a fresh analysis subagent, processed in the task ID order. You should maintain a maximum of `concurrency` (from the prepare output or `manifest.json`) subagents running in parallel. The task Markdown is the **complete contract for each subagent**. Invoke and delegate the next task to a new subagent when a task completes.

   Track each delegation by task ID. After a subagent finishes, confirm that `<run-dir>/results/<task-id>.json` exists. If the subagent fails, times out, or returns without that result file, delegate the same task to a fresh subagent before moving to the next task. Continue until every prepared task has a result file.

5. Finalize the scan:

   ```bash
   python3 <skill-dir>/scripts/scan.py finalize <run-dir>
   ```

   If finalization identifies missing or invalid task results, retry only those tasks and finalize again. The scan is complete when finalization reports `complete` and writes `report.json`.

## Run Directory

Each scan stores its artifacts under `<repo-path>/.vas/<VAS-ID>/run_<timestamp>/`:

```text
<run-dir>/
├── manifest.json                              # Task list and scan configuration (concurrency, etc.)
├── repository_overview.md                     # Snapshot of the repository overview
├── tasks/
│   └── TASK-0001.md                           # Complete contract (subagent instruction) for one analysis task
├── shared_checks/
│   └── <repository-relative-file>.md           # Reusable facts recorded during analysis (mirrorred file path from the repository)
├── results/
│   └── TASK-0001.json                         # Reports produced by one completed task
└── report.json                                # Final deduplicated report created by finalize
```

## Output

Return the report path and report count to the user. If execution cannot complete, identify the failed phase and preserve the run directory so the scan can be resumed.
