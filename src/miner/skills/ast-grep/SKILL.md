---
name: ast-grep
description: Compile structural intents into ast-grep patterns or YAML rules and validate them.
---

# Role & Task

Translate one supplied structural intent into the smallest faithful ast-grep query. Own query mechanics only. Preserve the intent's required matches and structural distinctions without inventing higher-level semantics.

# Workflow

## Step 1: Interpret the structural intent

- Read `references/experiences.md` before constructing the first candidate. Treat its lessons as heuristics and revalidate them for the current language, query, and ast-grep version.
- Identify the smallest AST node that directly expresses the requested structure.
- Separate required syntax variants from incidental identifiers, comments, enclosing scopes, and neighboring operations.
- Treat supplied positive examples and target sites as validation evidence. When the intent is underspecified, prefer the least assumptive recall-preserving interpretation and report the ambiguity to the caller.

## Step 2: Choose the query form

- Use a raw `pattern` for one concrete AST shape.
- Use a YAML `rule` when matching requires node kinds, relational constraints, boolean composition, regex, or contextual pattern parsing.
- Follow the caller's contract for top-level `id` and `language`. When the caller supplies them, return a `rule:` body without duplicating those fields.
- Start with one concrete shape, generalize only for required variants, and add only constraints supported by the structural intent.

## Step 3: Construct a valid structural match

- Make every string-form `pattern` parse as exactly one AST node. Represent alternative nodes with `any`, or relate one target node to another with `precedes` or `follows`.
- Use only Rule Object keys such as `pattern`, `kind`, `regex`, `inside`, `has`, `precedes`, `follows`, `all`, `any`, and `not` inside `rule:`. Place `constraints` beside `rule:`, never inside a nested Rule Object.
- Never use `regex` as the only positive atomic matcher. Pair it with `kind` so ast-grep has a bounded set of AST node kinds to test.
- Use `$NAME` for one named node, `$$TOKEN` for one unnamed node, and `$$$NODES` for zero or more nodes. Every metavariable must occupy a complete AST node; embedded text such as `obj.on$EVENT` is not a metavariable match.
- Add `stopBy: end` to `inside` and `has` unless the intent requires a nearer boundary.
- When a fragment needs surrounding syntax to parse correctly, use the object form of the atomic `pattern` rule inside a YAML rule. Its `context` is the parseable snippet and its `selector` is the node to match. Keep relational rules such as `inside` and `has` as separate Rule Object fields.
- Prefer structural constraints over exact identifiers, while preserving any discriminative operation explicitly required by the intent.

## Step 4: Validate and refine

- Use the query-execution interface supplied by the caller. Treat its schema, allowed targets, output limits, and timeout as authoritative. Do not assume a shell, programming-language executable, tool name, or filesystem layout.
- Start with counts or bounded representative matches. Request complete matches and metavariable captures only when smaller results cannot validate the intent.
- Validate every supplied positive example before inspecting a broader target corpus. Treat zero matches as query evidence and correct explicit syntax or execution errors before revising the query.
- Before accepting a raw pattern, run it with `debug_query=pattern` and confirm that the matcher root and metavariables represent the intended construct. Use `ast` or `sexp` for named Tree-sitter structure and `cst` when unnamed nodes matter; inspect the complete verbatim stderr. In C, function-like fragments without statement context can parse as declarations or macro type specifiers instead of call expressions.
- `debug_query` applies only to raw pattern queries. For a string-form pattern nested in a YAML rule, debug the pattern separately. For an object-form pattern, debug its full `context` snippet to inspect the intended `selector` node, then run the complete rule normally to validate the selector and other constraints.
- Refine only to satisfy the structural intent or preserve a required distinction. Do not iteratively remove unrelated matches by adding incidental project context. Read only the relevant section of `references/rule_reference.md` when syntax details are uncertain, and stop once required matches and grounding are established.

## Step 5: Report query-writing experience

- Return an empty experience list by default. Report an experience only when substantive query or debugging work reveals concise, reusable guidance. A straightforward successful query should not produce an experience.
- Compare against `references/experiences.md` first. Do not restate an existing lesson. If a lesson needs a new caveat, use `REPLACE` with its exact `lesson_id` and preserve the existing wording while appending the caveat.
- Each experience contains exactly `mode` (`ADD` or `REPLACE`), `lesson_id` (`ALL-N` or `<LANGUAGE>-N`), and a short, self-contained `lesson`. Use `ALL` for language-agnostic guidance and the uppercase ast-grep language name for language-specific guidance.
- Return no more than three distinct experience updates for one Synthesizer output; the host uses only the final complete output after any repair/resume.
- `ADD` uses the next unused ID in its section. `REPLACE` updates the existing lesson with that ID. Lessons are stored as `- [lesson_id] lesson` under the matching language section.
- Keep lessons generic, project-independent, concise, and expressed in 1-2 sentences. Support them with stderr, debug trees, or match results. Exclude repository paths, issue or intent semantics, match counts, routine validation outcomes, tool narration, and query transcripts. State the language or query form when it materially affects the lesson.

# Constraints

- Do not depend on caller-specific field names, schemas, roles, or output models.
- Do not redefine the structural intent or turn the query into a higher-level defect verdict.
- Do not encode project-specific identifiers or enclosing scopes unless the structural intent explicitly requires them.
- Never sacrifice a required positive match for precision.
