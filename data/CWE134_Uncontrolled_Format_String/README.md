# CWE-134 Juliet-style Example Suite

This is a small, synthetic VAMiner input inspired by the organization and naming conventions of the NIST Juliet Test Suite. It is not copied from the Juliet distribution.

Both C files express the same defect pattern: text read from standard input reaches `printf` as its format argument. The safe contrast passes the same text as a value argument to the constant format string `%s`.

- Flow variant `01` keeps the source and sink in one function.
- Flow variant `41` passes data to a sink function in the same source file.
- `manifest.json` records the intended bad and good functions. These labels are navigation hints; VAMiner still verifies behavior from source.

Use it from the repository root:

```bash
uv run python -m src.miner.main --example-suite data/CWE134_Uncontrolled_Format_String
```

Each source file can also be compiled independently for inspection:

```bash
cc -std=c11 -Wall -Wextra -pedantic -DINCLUDEMAIN \
  data/CWE134_Uncontrolled_Format_String/CWE134_Uncontrolled_Format_String__char_stdin_printf_01.c \
  -o /tmp/cwe134_01
```

Do not use this intentionally vulnerable code in production.
