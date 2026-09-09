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

- Read `references/experiences.md` before constructing the first candidate.
  Treat its evolving lessons as heuristics and revalidate them for the current
  language, query, and ast-grep version.
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
- Never use `regex` as the only positive atomic matcher. Pair it with `kind` so
  ast-grep has a bounded set of AST node kinds to test.
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
- Before accepting a raw pattern, run it once with `debug_query=pattern` and
  confirm that ast-grep's matcher root and metavariables represent the intended
  construct. Use `ast` or `sexp` for the named Tree-sitter structure and `cst`
  when unnamed nodes matter; inspect the complete verbatim stderr. In C,
  function-like fragments without statement context can parse as declarations
  or macro type specifiers instead of call expressions.
- `debug_query` applies to raw pattern queries. To debug a pattern nested in a
  YAML rule, test that pattern separately with the same language and context.
- Refine only to satisfy the structural intent or preserve a required
  distinction. Do not iteratively remove unrelated matches by adding incidental
  project context.
- Read only the relevant section of `references/rule_reference.md` when syntax
  details are uncertain.
- Stop once required matches and target grounding are established.

## Step 5: Report query-writing experience

- Return an empty experience list by default. Return at most three lessons only
  when this run establishes materially new, reusable ast-grep query-construction
  or query-debugging guidance.
- Compare against `references/experiences.md` first. Never restate or paraphrase
  an existing lesson. If new evidence only adds a caveat, extend the existing
  lesson by preserving its wording and appending the caveat, so the host can
  replace it instead of creating a neighboring entry.
- Record only `success` or `pitfall` observations supported by this run's
  stderr, debug tree, or match results. State the language or query form when it
  matters.
- Exclude repository paths, issue or intent semantics, match counts, routine
  validation outcomes, tool-usage narration, and transcripts of attempted
  queries.

# Constraints

- Do not depend on caller-specific field names, schemas, roles, or output
  models.
- Do not redefine the structural intent or turn the query into a higher-level
  defect verdict.
- Do not encode project-specific identifiers or enclosing scopes unless the
  structural intent explicitly requires them.
- Never sacrifice a required positive match for precision.
