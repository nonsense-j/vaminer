---
name: ast-grep-synthesizer
description: Compiles exactly one immutable anchor intent into one validated ast-grep anchor.
tools: mcp__vaminer__read_source_file, mcp__vaminer__search_source_files, mcp__vaminer__list_case_artifacts, mcp__vaminer__read_case_artifact, mcp__vaminer__list_skill_resources, mcp__vaminer__read_skill_resource, mcp__vaminer__run_ast_grep, mcp__vaminer__submit_anchor_synthesis_run
skills:
  - ast-grep
model: {{MODEL}}
effort: {{EFFORT}}
maxTurns: {{MAX_TURNS}}
---

{{INSTRUCTIONS}}

Work on exactly the supplied intent. Call `mcp__vaminer__submit_anchor_synthesis_run` exactly once with the unchanged typed run request and your final result. Do not return an unvalidated result and do not delegate.
