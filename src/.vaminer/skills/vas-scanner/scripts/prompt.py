TASK_TEMPLATE = """# VAS Candidate Analysis Task

## Task

You are assigned to analyze *a candidate file* for a specific defect detection rule. You need to carefully review that candidate file and its related source in the repository to determine whether the defect is present, then record the result before ending the task.

- Task ID: `{task_id}`
- Rule ID: `{rule_id}`
- Category: `{rule_category}`
- Repository: `{repository}`
- Candidate file: `{candidate_file}`
- Run directory: `{run_dir}`
- Shared checks directory: `{shared_checks_path}`
- Repository overview: `{overview_path}`


## Rule Specification

### Summary

{rule_summary}

### Scenarios

> Here are some example scenarios that illustrate the defect and its safe handling. Treat them as guidance for your analysis, not as a complete list of all possible cases.

#### Unsafe Scenarios

{unsafe_scenarios}

#### Safe Scenarios

{safe_scenarios}


## Candidate Anchor Hints

> Inspect every listed anchor in the candidate file, which has hints designed to help you navigate the code and review the relevant evidence.

{anchor_hints}


## Analysis Constraints

### Workflow

1. Read the overview and rule specification.
2. Inspect the candidate to identify potential rule-violated defects. Pay aettention to every listed anchors.
3. During analysis, before opening a related file, **always first check their mirrored Markdown path** under the Shared Checks Root (e.g., `shared_checks/a/b/c/d.md`) and directly use existing safe or alert facts to reduce redundant analysis.
4. For each listed anchor, decide whether it is SAFE or ALERT and provide a concrete summarized fact answering the hint. For each defect, verify that each is supported by sufficient evidence and the defect chain is valid.
5. Record the facts and reports as one JSON object with `anchorFacts` and `reports`. Submit that object using the Bash heredoc below. If record reports a schema error, correct the JSON and retry. Once record succeeds, **end and return the success message with defect counts only**.

Replace `<your JSON object>` with the complete JSON object matching the Output Contract:

```bash
{record_command} <<'JSON'
<your JSON object>
JSON
```

> Record: The command will save the facts to Shared Checks and the reports to `{result_path}` if successful.
> Note: The analysis must be thorough and results are supported by sufficient evidence.


## Output Contract

```json
{{
  "anchorFacts": [
    {{
      "anchorId": <string, anchor identifier>,
      "startLine": <integer, start line number, >= 1>,
      "status": <string, anchor status, e.g. SAFE>,
      "fact": <string, concrete English code fact answering the Hint>
    }}
  ],
  "reports": [
    {{
      "buggyFilePath": <string, file path where the defect is located>,
      "defectLevel": <integer, severity: 0=Fatal, 1=Critical, 2=General, 3=Warning, 4=Info>,
      "defectType": <string, infer the general vulnerability type from the rule specification>,
      "functionName": <string, function where the alert trigger point is located>,
      "mainBuggyLine": <integer, line number of the alert trigger point, >= 1>,
      "description": <string, inspection comment / vulnerability description>,
      "mainBuggyCode": <string, pre-fix code snippet, i.e. trigger point code>,
      "fixSuggestion": <string, fix suggestion>,
      "fixCode": <string, post-fix code snippet>,
      "events": [
        {{
          "description": <string, event description>,
          "line": <integer, line number, >= 1>,
          "main": <boolean, whether this is the defect trigger point; exactly one event in the whole chain must be true>,
          "path": <string, file path where the event is located>,
          "mainBuggyCode": <string, code snippet at this event>,
          "codeContext": <string, context code>
        }}
      ]
    }}
  ]
}}
```
"""
