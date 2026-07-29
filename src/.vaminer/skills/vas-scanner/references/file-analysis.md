# Rule-Guided File Analysis

Analyze one candidate file under one VAS rule. This reference owns the code-inspection and warning-decision rules; scanner scripts only provide discovery hints and manage scan artifacts.

## Inputs

- Repository path
- Candidate file
- Rule `summary`
- Rule `scenarios`
- Candidate hotspot Markdown

## Rule Interpretation

- Treat `summary` and `scenarios` as the complete defect specification.
- Treat each unsafe scenario as an independent, complete way the rule can be violated.
- Treat each safe scenario as independently sufficient to rule out an apparent violation; safe scenarios take precedence.
- Treat the candidate file as the starting point, not a boundary on where evidence or the actual violation may be located.
- Treat anchor `behavior`, `inspect_hint`, match locations, and priority scores only as navigation hints.

## Analysis Method

1. Read the summary and every unsafe and safe scenario before judging the code.
2. Start from the hotspot, then inspect the enclosing functions and other relevant regions of the candidate file.
3. Follow callers, callees, types, macros, configuration, control flow, data flow, or related files only when needed to resolve a scenario.
4. Keep cross-file tracing bounded to the evidence chain relevant to this rule, and stop when the applicable unsafe or safe scenario is resolved.
5. Check the safe scenarios before emitting a warning.
6. Emit a warning only when concrete code evidence supports one unsafe scenario and no safe scenario applies.

## Inspection Constraints

- Keep the repository read-only.
- Do not audit unrelated defects or general code quality.
- Do not require the target to reproduce the original issue's names, symbols, files, or structure.
- Do not produce a verdict for each anchor; an anchor match is never sufficient evidence by itself.
- Do not infer facts that the inspected code does not establish.
- Return `[]` only after completing the analysis and finding no warning. If analysis cannot be completed, surface the failure to the coordinator instead.

## Confidence

- `HIGH`: the evidence chain fully satisfies one unsafe scenario and no safe scenario applies.
- `MEDIUM`: the code probably satisfies an unsafe scenario, but one non-core fact requires a reasonable inference.
- `LOW`: the code resembles an unsafe scenario, but important supporting evidence remains weak.

## Output

Return only a JSON array. Return `[]` when the completed analysis produces no warning.

Each warning must contain:

```json
{
  "title": "Short warning title",
  "confidence": "HIGH",
  "primary_location": {
    "file": "repository/relative/path.c",
    "start_line": 10,
    "end_line": 10
  },
  "explanation": "Why one unsafe scenario applies and no safe scenario rules it out.",
  "evidence": [
    {
      "file": "repository/relative/path.c",
      "start_line": 8,
      "end_line": 10,
      "fact": "Concrete fact established from the code."
    }
  ]
}
```

Point `primary_location` to the actual violating operation, even when it is outside the original candidate file. Keep every evidence item concrete and repository-relative.
