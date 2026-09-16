# AST-Grep Query-Writing Experiences

Read these compact, non-redundant query-writing lessons before constructing a query. Treat them as heuristics and validate them against the current language and ast-grep version. Add or modify a lesson only only when valuable and needed.

## Language-Agnostic Lessons

- [ALL-1] A rule that uses `regex` must also constrain a set of AST node kinds, commonly with `kind`; regex alone is rejected before scanning.
- [ALL-2] For an ambiguous or empty raw pattern, call `debug_ast_grep_pattern` with `debug_query=pattern` first to preserve ast-grep metavariables; use `ast`, `sexp`, or `cst` only for deeper Tree-sitter structure, and read stderr verbatim.

## C Query Lessons

- [C-1] In C, a bare pattern such as `memcpy($$$ARGS)` can parse as a `macro_type_specifier` and match no calls; add enough statement context, such as a trailing semicolon, or use an object-form contextual pattern.
