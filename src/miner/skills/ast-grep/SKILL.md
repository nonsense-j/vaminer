---
name: ast-grep
description: ast-grep pattern and rule syntax, metavariables, relational constraints, and query debugging guidance.
---

# ast-grep Query Mechanics

Use this capability to implement an already-approved structural search intent. It defines query mechanics only; the calling agent owns the rule semantics and allowed intent.

The capability includes `run_ast_grep`, a minimal execution helper. Give it any directory inside the active workspace, a raw pattern or YAML rule body, and one output mode:

- `count` returns match and matched-file counts.
- `sample` returns counts and representative normalized code sites.
- `full` returns every normalized site and metavariable capture; use it only when the complete result is needed.

The helper reports observed syntax and matches. It does not decide which directory to scan, whether a result satisfies an intent, or how a query should change.

## Choose the Smallest Query Form

- Use a raw `pattern` for one concrete AST shape.
- Use a YAML `rule` when the query needs `kind`, relational constraints, boolean composition, regex, or contextual pattern parsing.
- A rule body starts with `rule:` and omits top-level `id` and `language`; those are supplied by the caller.
- Start with the simplest concrete shape that matches the required original case, generalize it for required variants, then add only recall-preserving constraints supported by the approved behavior.

## Construct Correct Structural Matches

- Use `$NAME` for one named node, `$$TOKEN` for one unnamed node, and `$$$NODES` for zero or more nodes.
- Metavariables must occupy a complete AST node; embedded text such as `obj.on$EVENT` is not a metavariable match.
- Add `stopBy: end` to `inside` and `has` unless an explicit nearer boundary is part of the intended relationship.
- Use an object-form pattern with `context` and `selector` when a fragment is ambiguous outside its surrounding syntax.
- Prefer structural constraints over exact project identifiers, but do not replace a discriminative operation with a generic call, assignment, condition, or definition.

## Diagnose and Refine

- When syntax details are uncertain, read only the relevant section of `references/rule_reference.md` with `skill_read_file`.
- Run the query on the directories that matter to the calling task. Start with `count` or `sample`; request `full` only when the smaller result cannot answer the question.
- Treat zero matches as evidence about the query, not as an execution failure. Correct explicit runner failures before drawing conclusions.
- Treat each query, immutable behavior, inspection hint, `behavior_weight`, and `query_weight` as one aligned configuration. A query change must preserve the approved intent and every required match.
