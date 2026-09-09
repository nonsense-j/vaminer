# AST-Grep Query-Writing Experiences

Read these compact, non-redundant query-writing lessons before constructing a
query. Treat them as heuristics and validate them against the current language
and ast-grep version. Add a lesson only when existing guidance cannot be
materially extended instead.

- **pitfall**: In C, a bare pattern such as `memcpy($$$ARGS)` can parse as a `macro_type_specifier` and match no calls; add enough statement context, such as a trailing semicolon, or use an object-form contextual pattern.
- **pitfall**: A rule that uses `regex` must also constrain a set of AST node kinds, commonly with `kind`; regex alone is rejected before scanning.
- **success**: For an ambiguous or empty raw pattern, inspect `debug_query=pattern` first to preserve ast-grep metavariables; use `ast`, `sexp`, or `cst` only for deeper Tree-sitter structure, and read stderr verbatim.
