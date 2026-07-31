---
name: ast-grep
description: Compile structural matching intents into ast-grep patterns or YAML rules and validate their syntax and matching results. Use for ast-grep query construction, metavariables, relational constraints, contextual patterns, bounded query testing, and query debugging.
---

# Role & Task

Translate one supplied structural intent into the smallest faithful ast-grep
query. Own query mechanics only. Preserve the intent's required matches and
structural distinctions without inventing higher-level semantics.

# Workflow

## Step 1: Interpret the structural intent

- Identify the smallest AST node that directly expresses the requested
  structure.
- Separate required syntax variants from incidental identifiers, comments,
  enclosing scopes, and neighboring operations.
- Treat supplied positive examples and target sites as validation evidence.
  When the intent is underspecified, prefer the least assumptive
  recall-preserving interpretation and report the ambiguity to the caller.

## Step 2: Choose the query form

- Use a raw `pattern` for one concrete AST shape.
- Use a YAML `rule` when matching requires node kinds, relational constraints,
  boolean composition, regex, or contextual pattern parsing.
- Follow the caller's contract for top-level `id` and `language`. When the
  caller supplies them, return a `rule:` body without duplicating those fields.
- Start with one concrete shape, generalize only for required variants, and add
  only constraints supported by the structural intent.

## Step 3: Construct a valid structural match

- Make every string-form `pattern` parse as exactly one AST node. Represent
  alternative nodes with `any`, or relate one target node to another with
  `precedes` or `follows`.
- Use only Rule Object keys such as `pattern`, `kind`, `regex`, `inside`, `has`,
  `precedes`, `follows`, `all`, `any`, and `not` inside `rule:`. Place
  `constraints` beside `rule:`, never inside a nested Rule Object.
- Use `$NAME` for one named node, `$$TOKEN` for one unnamed node, and
  `$$$NODES` for zero or more nodes. Make every metavariable occupy a complete
  AST node; embedded text such as `obj.on$EVENT` is not a metavariable match.
- Add `stopBy: end` to `inside` and `has` unless the intent requires a nearer
  boundary.
- Use an object-form pattern with `context` and `selector` when a fragment is
  ambiguous without surrounding syntax.
- Prefer structural constraints over exact identifiers, while preserving any
  discriminative operation explicitly required by the intent.

## Step 4: Validate and refine

- Use the query-execution interface supplied by the caller. Treat its schema,
  allowed targets, output limits, and timeout as authoritative. Do not assume a
  shell, programming-language executable, tool name, or filesystem layout.
- Start with counts or bounded representative matches. Request complete matches
  and metavariable captures only when smaller results cannot validate the
  intent.
- Validate every supplied positive example before inspecting a broader target
  corpus.
- Treat zero matches as query evidence, not an execution failure. Correct
  explicit syntax or execution errors before revising the query.
- Refine only to satisfy the structural intent or preserve a required
  distinction. Do not iteratively remove unrelated matches by adding incidental
  project context.
- Read only the relevant section of `references/rule_reference.md` when syntax
  details are uncertain.
- Stop once required matches and target grounding are established.

# Constraints

- Do not depend on caller-specific field names, schemas, roles, or output
  models.
- Do not redefine the structural intent or turn the query into a higher-level
  defect verdict.
- Do not encode project-specific identifiers or enclosing scopes unless the
  structural intent explicitly requires them.
- Never sacrifice a required positive match for precision.
