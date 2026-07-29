# Claude CLI Runtime and Example-Suite Input Plan

## Summary

Finish and optimize the current worktree rather than rewriting it. Deliver:

- Claude Code CLI as a first-class runtime alongside Pydantic AI, with equivalent typed tasks, validation, limits, usage reporting, and no fallback.
- `example suite` input support that skips issue collection and shares the RCA, Rule Generator, validation, review, and persistence pipeline.
- A unified `RootCauseAnalysis` contract for issue and example-suite inputs.
- A Rule Generator matching `main`: it remains the general executor and delegates anchor-query synthesis to isolated specialist agents.

Reserve these terms:

- **Example suite**: input directory containing related good/bad examples.
- **Example**: an individual input file, function, or labeled behavior.
- **Case**: an RCA-generated `caseN` or `caseN_varM` artifact only.

## Public Contracts

- Replace the uncommitted `--case-dir` interface with `--example-suite PATH`; do not retain a legacy alias.
- Use `example_suite` as the tagged source type.
- Rename input-specific types to `ExampleSuiteIntake`, `ExampleSuiteWorkflow`, and `ExampleSuiteVASSource`.
- Keep `RootCauseAnalysis` for both source types:
  - `buggy_components` contains source-root-relative causal spans.
  - `fixing_pattern` contains the observed fix or an explicitly inferred invariant.
  - `extracted_case_files` contains generated RCA cases.
- Use `AnalysisSubject` to distinguish:
  - `issue` with `repo_evidence`;
  - `example_suite` with `bad_span_coverage`.
- Make `TaskContext.source_root` the authoritative readable tree. Populate `repo_path` only for real Git repositories.
- Replace the separate Rule Planner phase with a Rule Generator phase returning complete `VASCoreInfo`.

## Implementation

### Unified RCA and example-suite intake

- Consolidate the two RCA output models and validators around `RootCauseAnalysis`.
- Use one Root Cause Analyzer instruction with an explicit intake section:
  - For an example suite, use comments and good/bad/CWE labels from both good and bad examples as untrusted comparison and navigation hints. Prove conclusions from code behavior.
  - For an issue with a fixed revision, inspect the narrow repository diff first; diff and source evidence outrank issue prose.
  - Without a fixed revision, use bounded source search and mark the fixing invariant as inferred.
- Expose `read_fixed_diff` only when issue collection supplied a fixed revision.
- Validate every RCA component for relative-path containment, existence, line bounds, and snippet agreement.
- For example suites, require the full declared bad-span set to be covered by synthesized anchors and emit no repository evidence.
- Validate suite size, file count, language, symlinks, filesystem entries, and content digest.
- Copy through a staging directory, re-inspect the copy, atomically publish the snapshot, then atomically update registries. Recover missing or partial registered snapshots on reuse.
- Publish portable provenance only: registry key, suite name, digest, relative snapshot reference, file metadata, and RCA summary. Never publish absolute paths.

### Shared workflow and Rule Generator

- Extract one post-RCA workflow used by issues and example suites. It owns Rule Generator execution, cache identity, deterministic final validation, review, and VAS persistence.
- Keep source-specific workflows responsible only for intake, RCA setup, provenance, and tagged source assembly.
- Restore the `main` Rule Generator behavior:
  1. Read the complete RCA and all generated cases.
  2. Produce the rule, scenarios, and queryless anchor intents.
  3. Delegate every anchor query to an isolated AST-grep Synthesizer.
  4. Consume only deterministically validated synthesis results.
  5. Return complete `VASCoreInfo`.
- For Pydantic AI, restore the typed `synthesize_ast_grep_anchors` tool. It fans out one Synthesizer agent per intent, accumulates child usage, and calls the shared deterministic aggregator.
- Preserve cache/runtime identity across both parent and synthesis agents. Reject unintended runtime or model drift.

### Claude-native delegation

- Build an invocation-scoped `vaminer` plugin from repository-owned sources; never install it globally.
- Package:
  - `vaminer:rule-generator` as the phase’s main agent;
  - `vaminer:ast-grep-synthesizer` as its only allowed subagent;
  - `vaminer:ast-grep` as the Synthesizer’s preloaded skill.
- Run Rule Generation with `--plugin-dir` and `--agent vaminer:rule-generator`.
- Allow only `Agent(vaminer:ast-grep-synthesizer)`; no unrestricted Agent/Task access.
- Give the Synthesizer exact source-read, case-read, skill-reference, AST-grep, and typed-result-submission MCP tools. Do not expose Bash, Write, Edit, or nested delegation.
- Add invocation-local typed MCP gates:
  - `submit_anchor_synthesis_run` validates and records one result against its immutable intent.
  - `finalize_anchor_synthesis_batch` requires exactly one validated result per intent, runs deterministic recall/grounding scans, and returns `AnchorSynthesisResult`.
- Restrict subagent depth to one and bound model, effort, turns, concurrency, and total subagent count.
- Record subagent events and aggregate their usage into the Rule Generator task result.

### Claude CLI parity and isolation

- Compile built-ins, permissions, MCP registration, plugin bindings, prompt bindings, and audit metadata from one immutable task policy.
- Register only policy-authorized MCP tools.
- Default to OAuth-compatible isolated mode:
  - omit `--bare`;
  - use empty user/project/local setting sources;
  - use strict generated MCP configuration;
  - disable auto-memory, prompt history, inherited Claude.ai MCP servers, and subprocess environment leakage;
  - disable session persistence.
- Retain explicit bare mode for API-key or `apiKeyHelper` deployments.
- Sanitize explicit settings through an allowlist; never copy hooks, plugins, MCP, agents, permissions, or unknown settings.
- Use stream JSON, JSON Schema output, model/effort selection, native turn and budget limits, timeout/process cleanup, and bounded output capture.
- Treat terminal budget, maximum-turn, permission, provider, and protocol errors as authoritative even if earlier events contained valid-looking output.
- Aggregate requests, turns, tokens, cache tokens, cost, duration, and model usage across repair attempts and subagents.
- Never fall back from Claude Code to Pydantic AI.

## Test Plan

- Preserve the current green baseline and add coverage for:
  - unified RCA output for issue and example-suite inputs;
  - fixed-diff-first issue intake and good/bad example guidance;
  - example-suite tasks using `source_root` without fake repository state;
  - complete bad-span coverage and path/snippet validation;
  - staged intake, digest conflicts, interrupted copies, stale registries, and recovery;
  - portable `example_suite` VAS JSON without absolute paths;
  - one shared post-RCA workflow and no issue collection for `--example-suite`;
  - Pydantic typed-tool synthesis delegation;
  - Claude plugin generation, exact Agent restriction, skill preload, typed per-run submission, and deterministic batch finalization;
  - exact MCP `tools/list` visibility and absence of generic shell/edit tools;
  - OAuth-compatible isolation, explicit bare mode, sanitized settings, decreasing repair-attempt turn budgets, authoritative terminal failures, aggregated usage, and no fallback.
- Run:
  - `uv run --frozen pytest -q`
  - `uv run --frozen python -m compileall -q src test`
  - focused Ruff checks already used by the repository
  - `git diff --check`
- After deterministic tests pass, run one bounded, no-cache Claude CLI smoke using the installed authenticated CLI. Record CLI/model identity, delegation events, tool policy, usage/cost, final output, and private diagnostic artifact paths.

## Assumptions

- The current worktree is an uncommitted implementation, so legacy `case bundle` names can be replaced without a compatibility alias.
- Generated RCA artifacts retain `caseN` naming.
- Pydantic AI remains supported as an equal peer runtime.
- No Agent SDK dependency, general shell access, multi-language suite support, runtime fallback, or global Claude plugin installation is introduced.
- Preserve unrelated worktree changes and optimize the existing implementation in cohesive increments.
