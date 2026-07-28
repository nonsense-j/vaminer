---
name: vas-scanner
description: Run a rule-guided repository scan using a Variant Analysis Specification (VAS). Discover and prioritize candidate files from rule-sensitive anchors, analyze every candidate against the rule summary and scenarios, and produce a deduplicated JSON warning report.
---

# Rule-Guided VAS Scan

Run one VAS rule against one repository. Use anchors to find and prioritize candidate files, then judge each candidate using the rule `summary` and `scenarios`.

## Responsibilities

- `scripts/scan.py` owns deterministic rule loading, anchor execution, candidate ranking, scan state, and report assembly.
- The coordinating agent owns batch dispatch and records each candidate result in rank order.
- [references/file-analysis.md](references/file-analysis.md) owns all code-inspection and warning-decision guidance.

## Inputs

- `vas_id`: an id present in `rules/<vas_id>.json`. Ask user if not provided.
- `repo_path`: the target repository directory.

## Workflow

### 1. Prepare

```bash
python3 <skill-dir>/scripts/scan.py prepare <vas_id> <repo_path>
```

Use the returned `scan_dir` for every later command. Preparation runs all anchor queries, ranks admitted files, and writes hotspot Markdown under the scan directory. A query that executes successfully with zero matches is valid; an anchor execution failure stops preparation.

### 2. Analyze the next batch

```bash
python3 <skill-dir>/scripts/scan.py next <scan-dir>
```

For every returned candidate:

1. Use one subagent per candidate when subagents are available; otherwise analyze sequentially.
2. Give the analyzer the returned `repo`, `candidate`, `summary`, `scenarios`, `hotspot`, and `analysis_reference` values.
3. Require the analyzer to follow `references/file-analysis.md` and return only its JSON warning array.

Complete the whole batch before calling `next` again.

### 3. Record or retry

Record successful analyses in candidate-rank order. Pass the analyzer's JSON array on stdin, including `[]` when a completed analysis finds no warning.

```bash
python3 <skill-dir>/scripts/scan.py record <scan-dir> <rank> <<'JSON'
<analyzer JSON array>
JSON
```

If analysis execution fails or its output cannot be recorded, pass the error on stdin and retry that candidate:

```bash
python3 <skill-dir>/scripts/scan.py retry <scan-dir> <rank> <<'ERROR'
<analysis execution error>
ERROR
```

Do not record `[]` for a failed analysis. Continue `next` → analysis → `record` or `retry` until `next` returns `"done": true`.

### 4. Finalize

```bash
python3 <skill-dir>/scripts/scan.py finalize <scan-dir>
```

Finalize only after every candidate is complete. The command deduplicates warnings, merges evidence and source candidates, and writes `report.json`.
