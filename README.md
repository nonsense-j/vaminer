# VAMINER

[中文说明](README_zh.md)

VAMINER converts a reported software issue into a Variant Analysis Specification (VAS) and packages generated rules with an agent skill that can find related defects in newer versions of the source repository or in other repositories.

The system follows a **sound search, precise analysis** model:

- Deterministic ast-grep anchors find and prioritize defect-relevant code hotspots.
- An analysis agent decides whether candidate code violates the rule's behavioral scenarios.

An anchor is only a retrieval and navigation signal. It is never a defect verdict.

## Scope

This repository generates VAS rules. It does **not** provide a standalone end-user rule runner. To scan another project for potential 1-day defects or vulnerabilities, install the bundled [`vas-scanner`](src/.vaminer/skills/vas-scanner/) skill in your coding agent and invoke the agent with a generated rule.

## Quick Start

### Requirements

- Python 3.12 or newer
- `uv`
- Git
- The `ast-grep` or `sg` executable available on `PATH`
- An API key for a supported LLM provider

### Install and configure

```bash
git clone <this-repository-url>
cd vaminer
uv sync
cp .env.example .env
```

The example selects DeepSeek. Set `DEEPSEEK_API_KEY` in the repository-root `.env`, or choose one of the other configurations in [LLM configuration](#llm-configuration).

Confirm that ast-grep is available before mining:

```bash
ast-grep --version
```

### Generate a rule

Pass a CVE ID or GitHub issue URL to the miner:

```bash
uv run python -m src.miner.main CVE-2024-XXXX
```

```bash
uv run python -m src.miner.main https://github.com/owner/repository/issues/123
```

Use `--use-cache` to reuse valid outputs from a previous attempt:

```bash
uv run python -m src.miner.main --use-cache CVE-2024-XXXX
```

The final rule is written to:

```text
src/.vaminer/skills/vas-scanner/rules/VAS-XXXX.json
```

Supporting analysis, cases, cache data, and anchor reports are written under `../vas_ws/miner/VAS-XXXX/`. Run logs are written under `logs/`.

## Run a Generated Rule on Another Project

The generated JSON rule is consumed by an agent through the `vas-scanner` skill; there is no separate runner to start in this repository.

1. Copy the complete skill directory into a skill folder recognized by the agent in the target project. For example:

   ```bash
   mkdir -p /path/to/target-project/.agent/skills
   cp -R src/.vaminer/skills/vas-scanner \
     /path/to/target-project/.agent/skills/
   ```

   Copy the whole directory, not only `SKILL.md`. It contains the scanner scripts, analysis guidance, and every generated rule in the `rules/` subdirectory. If the skill was installed before a new rule was generated, copy the new `VAS-XXXX.json` into the installed skill's `rules/` directory or reinstall the skill.

2. Ensure `ast-grep` or `sg` is available on the coding agent's `PATH`.

3. Open the target project with the agent and invoke the installed skill with the desired rule:

   ```text
   /vas_scanner Run VAS-XXXX rule on this project and find potential defects/vulnerabilities.
   ```

   Replace `VAS-XXXX` with the generated rule ID. If your agent uses a different skills directory or invocation syntax, use its equivalent while keeping the complete `vas-scanner` directory intact.

The skill deterministically uses the rule's ast-grep anchors to rank candidate files, asks the agent to analyze each candidate against the rule scenarios, and writes the final `report.json` under:

```text
<target-project>/.vas/VAS-XXXX/run_<timestamp>/report.json
```

## Mining Workflow

The miner runs a deterministic sequence:

1. **Issue Collection** gathers issue text, repository provenance, and buggy/fixed commits.
2. **Root Cause Analysis** identifies the concrete defect behavior and fixing pattern, then extracts minimal original and variant cases.
3. **Rule Generation** produces the rule summary, independent unsafe/safe scenarios, and one queryless anchor intent for each distinct rule-sensitive causal-chain site.
4. **AST-Grep Synthesis** runs each intent in an isolated, bounded Synthesizer context, then deterministically aggregates and re-scans the anchor batch against all cases and the buggy repository.
5. **Assembly** accepts the validated anchors atomically and emits the VAS rule.
6. **Post-generation Anchor Report** independently renders case coverage and repository hotspot results.

The Rule Generator never loads the ast-grep skill or edits a validated anchor. The AST-Grep Synthesizer exclusively owns query syntax, query/behavior alignment, and query validation.

The Agent runtime is Pydantic AI over OpenAI-compatible Chat Completions. The three main Agents remain Issue Collector, Root Cause Analyzer, and Rule Generator; the Rule Generator fans the typed synthesis request into one AST-Grep Synthesizer run per intent. Each run sees only its own immutable intent and has an independent dependency context and exploration budget. Deterministic orchestration restores request order, derives exact case coverage and canonical repository evidence from fresh scans, and accepts the batch atomically.

### Miner module responsibilities

- `src/miner/main.py` orchestrates pipeline stages, cache acceptance, assembly, and persistence.
- `src/miner/core/agents.py` constructs Agents and wires the Synthesizer handoff.
- `src/miner/core/capabilities.py` defines reusable capability bundles and local Skill loading.
- `src/miner/core/context.py` carries parent workflow state and isolated per-anchor run state.
- `src/miner/core/validation.py` defines deterministic acceptance checks reused by live outputs and cached outputs.
- `src/miner/anchors/review.py` renders post-generation anchor reviews; it does not own pipeline validation.
- `src/miner/tools/` contains external evidence, repository, and Pydantic output adapters.
- `src/miner/anchors/scanner.py` loads the scanner engine directly from the deployable `vas-scanner` skill.

### LLM configuration

All Agents reuse one process-wide Pydantic AI model returned by `get_llm()`.

Native DeepSeek:

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=...
```

Official OpenAI:

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.2
OPENAI_API_KEY=...
```

An OpenAI-compatible Chat Completions endpoint:

```dotenv
LLM_PROVIDER=openai-compatible
LLM_MODEL=your-model
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://your-endpoint/v1
```

`OPENAI_BASE_URL` is required only for a compatible endpoint. `LLM_PROVIDER` and `LLM_MODEL` are required.

External evidence and optional tracing are configured in the same ignored repository-root `.env`:

```dotenv
GITHUB_TOKEN=...

LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
```

`GITHUB_TOKEN` is optional but authenticates GitHub API requests and increases rate limits.

The Issue Collector's deferred `web-search` and `web-fetch` capabilities use local fallbacks and require no search-service API key. Specialized CVE and GitHub tools remain the preferred evidence sources.

#### Agent request budget

Each Agent run allows at most 100 model requests by default. Override the limit in the repository-root `.env` when needed:

```dotenv
MINER_MAX_REQUESTS_PER_AGENT=100
```

The Issue Collector and Root Cause Analyzer each have an independent budget. The Rule Generator reports accumulated usage from its child runs and remains subject to this overall budget when it resumes. Each per-anchor Synthesizer run additionally has code-owned limits of 32 model requests and 64 total tool calls. At most four anchor runs execute concurrently, and one synthesis request contains at most eight intents.

#### Proxy access

In network environments where DuckDuckGo cannot be reached directly, including some connections from mainland China, configure the standard proxy variables in the repository-root `.env`:

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

No additional proxy configuration is required. VAMINER loads these variables before constructing its HTTP clients and bridges the HTTPS proxy (falling back to the HTTP proxy) to the local web-search client. `NO_PROXY` keeps loopback traffic direct. These variables are process-wide, so model-provider, GitHub, web-search, and web-fetch requests may all use the configured proxy.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to enable optional tracing. `LANGFUSE_BASE_URL` is needed only for a custom Langfuse server; otherwise the SDK default is used. Other Miner behavior limits—filesystem reads, overflowing tool output, compaction, and ast-grep execution—are code-owned constants in `src/miner/configs.py`, not environment variables.

### Tests

Run the behavior and end-to-end suite with:

```bash
uv run pytest
```

## Rule Semantics

- `summary` is one general, normative software safety requirement.
- Each `scenarios.unsafe` entry is an independent, complete issue-derived defect scenario.
- Each `scenarios.safe` entry is an independent, complete scenario that rules out the defect and takes precedence over an apparent unsafe match.
- `anchors` match only rule-sensitive hotspot operations from the defect behavior. Fixing behavior is not an anchor.

```json
{
  "vas_id": "VAS-0007",
  "category": "SECURITY",
  "language": "c",
  "sources": [],
  "summary": "Security decisions based on hostnames must use their canonicalized representation.",
  "scenarios": {
    "unsafe": [
      "A security policy lookup compares a raw internationalized hostname with canonical stored entries before hostname normalization."
    ],
    "safe": [
      "The hostname is normalized before every security policy lookup, and each lookup receives the canonical value."
    ]
  },
  "anchors": [
    {
      "id": "hostname-policy-check",
      "behavior_weight": 5,
      "query_weight": 4,
      "type": "rule",
      "query": "rule:\n  any:\n    - pattern: policy_check($HOST)\n    - pattern: project_policy_check($HOST)",
      "behavior": "Performs a security policy decision using a hostname value.",
      "inspect_hint": "Trace whether the hostname is normalized before this policy decision."
    }
  ]
}
```

`behavior_weight` records the rule importance of the intended inspection behavior. `query_weight` may be lower when the recall-preserving query is a weaker or broader proxy, and it is the only weight used for ranking. File priority sums the query weights of distinct matched anchors; repeated matches from the same anchor add navigation locations but do not multiply the score. Per-intent `required_cases` exists only in the synthesis request and is not part of synthesized anchors or the final VAS schema.

## Anchor Quality

A valid anchor set is recall-complete and behavior-distinct:

- Every anchor covers its required original cases and transformation variants.
- Every anchor matches its corresponding RCA-declared site in the buggy repository.
- Each anchor represents one distinct observable behavior in the causal chain, even when case coverage overlaps.
- Generic calls, assignments, definitions, and conditions are rejected unless structural or API constraints make them rule-sensitive.
- Repository matches guide recall-preserving precision refinement; extra matches do not justify narrowing away required evidence.

See [the vas-scanner skill](src/.vaminer/skills/vas-scanner/SKILL.md) for its internal rule-execution and reporting workflow.
