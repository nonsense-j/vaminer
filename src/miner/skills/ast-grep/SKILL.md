---
name: ast-grep
description: Build and test ast-grep patterns and YAML rules with the packaged deterministic runner. Use for structural query construction, metavariables, relational constraints, bounded scans, and query debugging.
---

# ast-grep Query Mechanics

Use this skill to implement an already-approved structural search intent. It defines query mechanics only; the calling agent owns the rule semantics and allowed intent.

## Run a Query

Use the packaged runner at `scripts/runner.py`, relative to this `SKILL.md`. The operational binding supplies the Python executable and absolute skill root:

```text
"<python-executable>" "<skill-root>/scripts/runner.py" <target-dir> \
  --language <language> \
  --query-type pattern|rule \
  --query <query> \
  --output count|sample|full \
  [--sample-size N]
```

Scan only the source or case directories supplied by the task. The runner accepts:

- `count` returns match and matched-file counts.
- `sample` returns counts and representative normalized code sites.
- `full` returns every normalized site and metavariable capture; use it only when the complete result is needed.

Quote patterns so the shell does not expand ast-grep metavariables:

```bash
"<python-executable>" "<skill-root>/scripts/runner.py" "<case-dir>" \
  --language c --query-type pattern --query '$OBJ->$FIELD' --output sample
```

Pass a YAML rule as one quoted multiline argument:

```bash
"<python-executable>" "<skill-root>/scripts/runner.py" "<source-dir>" \
  --language c --query-type rule --output count \
  --query 'rule:
  all:
    - pattern: $CALL($ARG)
    - inside:
        kind: function_definition
        stopBy: end'
```

The runner reports observed syntax and matches. It does not decide whether a result satisfies the intent or how the query should change.

## Choose the Smallest Query Form

- Use a raw `pattern` for one concrete AST shape.
- Use a YAML `rule` when the query needs `kind`, relational constraints, boolean composition, regex, or contextual pattern parsing.
- A rule body starts with `rule:` and omits top-level `id` and `language`; those are supplied by the caller.
- Start with the simplest concrete shape that matches the required original case, generalize it for required variants, then add only recall-preserving constraints supported by the approved behavior.

## Construct Correct Structural Matches

- Every string-form `pattern` must parse as exactly one AST node. A sequence such as an assignment followed by an `if` statement is not one pattern; represent alternative single-node hotspots with `any`, or target one node and relate it to another with `precedes`/`follows`.
- Inside `rule:` and nested `all`/`any` entries, use only Rule Object keys such as `pattern`, `kind`, `regex`, `inside`, `has`, `precedes`, `follows`, `all`, `any`, and `not`. `constraints` is a RuleConfig-level sibling of `rule:`, never a field inside an `any` entry. Avoid constraints unless they are essential and their placement is certain.
- Prefer matching the smallest discriminative expression or statement. Do not include an entire `if` body merely to capture a guard expression when comments or peripheral statements can vary across required cases.
- Use `$NAME` for one named node, `$$TOKEN` for one unnamed node, and `$$$NODES` for zero or more nodes.
- Metavariables must occupy a complete AST node; embedded text such as `obj.on$EVENT` is not a metavariable match.
- Add `stopBy: end` to `inside` and `has` unless an explicit nearer boundary is part of the intended relationship.
- Use an object-form pattern with `context` and `selector` when a fragment is ambiguous outside its surrounding syntax.
- Prefer structural constraints over exact project identifiers, but do not replace a discriminative operation with a generic call, assignment, condition, or definition.

## Diagnose and Refine

- When syntax details are uncertain, read only the relevant section of `references/rule_reference.md` packaged with this skill.
- Run the query on the directories that matter to the calling task. Start with `count` or `sample`; request `full` only when the smaller result cannot answer the question.
- Prefer one aggregate scan of a case directory over separate scans of every file when the returned normalized sites identify all required files. The final VAS validator will perform fresh coverage and repository scans after parent assembly.
- Treat zero matches as evidence about the query, not as an execution failure. Correct explicit runner failures before drawing conclusions.
- Treat the immutable `behavior` as the semantic core of the query. The inspection hint guides post-match analysis rather than defining a full query.
- When the complete anchor plan is available, prefer the smallest target node that naturally distinguishes the target behavior from sibling behaviors. One precision pass may add local, defect-relevant structural context, such as requiring the target node to be inside an `if_statement`, when every required case and the RCA site support it.
- Do not add project identifiers, incidental enclosing scopes, exact expressions, or another anchor's complete behavior. Do not refine merely to remove unrelated repository matches.
- Repository precision work is limited to one optional refinement pass. If extra matches remain, keep the recall-preserving query and lower `query_weight`; do not iteratively eliminate unrelated matches.
- Stop querying and submit as soon as aggregate case recall and the required repository grounding are established; repeated confirmation or a second precision pass spends the caller's bounded request budget without adding required evidence.
