# C AST-Grep Query-Writing Experiences

- [C-1] In C, a bare pattern such as `memcpy($$$ARGS)` can parse as a `macro_type_specifier` and match no calls; add enough statement context, such as a trailing semicolon, or use an object-form contextual pattern.
