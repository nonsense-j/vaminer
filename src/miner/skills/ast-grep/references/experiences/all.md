# Language-Agnostic AST-Grep Query-Writing Experiences

- [ALL-1] A rule that uses `regex` must also constrain a set of AST node kinds, commonly with `kind`; regex alone is rejected before scanning.
- [ALL-2] Reusing one metavariable in two argument position (`f($A, $X, $B, $X)`) enforces textual/structure equality; wrapping it in `not:` excludes calls where thosepositions share one expression.
