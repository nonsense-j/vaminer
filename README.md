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
- For Pydantic AI, an API key for a supported LLM provider
- For Claude CLI, an installed `claude` command with an authenticated user session

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

You can also pass an Example Suite directory. Its basename (typically something like a CVE ID) is used as the registry identity. Files may be flat or arbitrarily nested, and all examples should demonstrate one shared defect pattern:

```bash
uv run python -m src.miner.main --example-suite /path/to/CVE-2024-XXXX
```

At the domain-input level, the miner only requires an existing non-empty directory containing at least one recognizable source file recursively. It imposes no case-count, layout, source-language-count, or manifest requirement. Good/bad evidence may be expressed through filenames, directories, comments, labels, or an optional manifest and is interpreted against source behavior during RCA. Symbolic links and special filesystem entries remain excluded so the immutable snapshot cannot escape the input directory.

Use `--use-cache` to reuse valid outputs from a previous attempt:

```bash
uv run python -m src.miner.main --use-cache CVE-2024-XXXX
```

The final rule is written to:

```text
src/.vaminer/skills/vas-scanner/rules/VAS-XXXX.json
```

The model workspace contains only source and generated cases:

```text
../vas_ws/miner/VAS-XXXX/
├── src/
└── cases/
```

Caches and diagnostics are kept outside the model workspace:

```text
output/
├── miner/VAS-XXXX/<input-id>/
│   ├── caches/                         # Issue Collection, RCA, and Rule Generation caches
│   └── anchor_review.md
└── logs/miner/VAS-XXXX/<input-id>/
    └── <trace-id>__<runtime>.log
```

`<trace-id>` is the overall Langfuse workflow trace id when tracing is enabled. Without Langfuse, VAMINER generates a local id with the same format. Set `VAMINER_OUTPUT_DIR` or pass `--output-dir` to relocate the complete `output/` tree.

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
3. **Rule Generation** produces the rule summary, independent unsafe/safe scenarios, and one queryless anchor intent for each distinct, local, rule-sensitive causal-chain site.
4. **AST-Grep Synthesis** runs each intent in an isolated, bounded Synthesizer context. A child returns only query fields for one target id; the host combines them with the canonical intent.
5. **Assembly and Validation** uses the authoritative RCA, latest accepted Anchor Plan, Rule Generation draft, and accepted query deltas to build the complete VAS.
6. **Post-generation Anchor Report** independently renders case coverage and repository hotspot results.

The Rule Generator never loads the ast-grep skill or authors query text. The AST-Grep Synthesizer exclusively owns query syntax and query/behavior alignment. If no trustworthy query can be produced, `query: ""` marks that anchor as disabled; it is skipped during scanning and ranking and is reported prominently in the anchor review and run log.

One mining run selects exactly one Runtime Adapter and one configured model. All phases, including child Synthesizers, retain that identity; there is no per-phase routing or runtime fallback. `VAMiner` accepts either an Issue or Example Suite through an Input Adapter, then uses one shared RCA → Rule Generation → persistence workflow.

`AnchorSynthesisSession` owns the authoritative RCA and latest successful Anchor Plan. It accepts at most two plans, starts one fresh child Agent per intent with concurrency capped at five, restores plan order, validates non-empty queries, and records only the latest successful batch. Children cannot return RCA, summary, behavior, inspect hints, or behavior weights. Each Synthesizer receives typed read-only source/case/skill tools and `run_ast_grep_query`; generic filesystem, shell, network, and further delegation are unavailable.

### Miner module responsibilities

- `src/miner/agent/` defines the closed Phase Authority and the small Runtime Seam.
- `src/miner/models/` contains issue, root-cause, anchor, and VAS models.
- `src/miner/mining/` owns Phase Definitions, Input Adapters, the shared VAMiner workflow, and deterministic acceptance.
- `src/miner/utils/` owns general configuration, workspace layout, typed cache persistence, logging, and telemetry.
- `src/miner/tools/` contains runtime-neutral evidence, repository, case, skill, and ast-grep operations.
- `src/miner/runtimes/shared/` contains the host-owned Anchor Synthesis Session.
- `src/miner/runtimes/pydantic/` contains the in-process Pydantic AI Adapter, LLM construction, hooks, and exact typed tools.
- `src/miner/runtimes/claude/` contains the Claude CLI Adapter, policy compiler, bounded subprocess decoder, and exact phase-scoped MCP tools.
- `src/miner/anchors/` contains generated-rule scanning and post-generation review.
- `src/miner/main.py` is the CLI composition root for runtime selection, workflow execution, assembly, and persistence.

### LLM configuration

The Pydantic AI adapter keeps the existing explicit provider configuration and reuses one process-wide model returned by `get_llm()`.

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

The Claude CLI adapter has a separate, intentionally narrower configuration:

```dotenv
MINER_AGENT_RUNTIME=claude-cli
CLAUDE_CODE_MODEL=claude-sonnet-4-6
```

You can also pass `--claude-model claude-sonnet-4-6`. VAMINER invokes Claude with the `user` setting source, strict temporary MCP configuration, and a fresh session id. When tracing is enabled, the session transcript exists only until Claude's synchronous Stop/SessionEnd hooks complete and is then deleted together with its tool-result directory. The child process inherits the complete parent environment for normal Claude authentication and provider selection; environment values are never written to logs or traces. Checkout project/local settings, instructions, and MCP configuration are not loaded.

To trace `claude-cli`, install the official Langfuse Observability Plugin once at Claude's user scope:

```bash
claude plugin marketplace add langfuse/Claude-Observability-Plugin
claude plugin install langfuse-observability@langfuse-observability
```

VAMINER passes the active phase span through `CC_LANGFUSE_TRACEPARENT`, so the plugin's Conversational Turn, Generation, and Tool observations join the same Miner trace. The plugin is disabled in VAMINER's temporary settings whenever the parent Langfuse trace is disabled or unavailable.

External evidence and optional tracing are configured in the same ignored repository-root `.env`:

```dotenv
GITHUB_TOKEN=...

LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
```

`GITHUB_TOKEN` is optional but authenticates GitHub API requests and increases rate limits.

The Issue Collector's deferred `web-search` and `web-fetch` capabilities use local fallbacks and require no search-service API key. Specialized CVE and GitHub tools remain the preferred evidence sources.

#### Agent turn budget

Each phase has its own model-turn budget. Override the limits in the repository-root `.env` when needed:

```dotenv
MINER_MAX_TURNS_ISSUE_COLLECTION=40
MINER_MAX_TURNS_ROOT_CAUSE=40
MINER_MAX_TURNS_RULE_GENERATION=30
MINER_MAX_TURNS_PER_ANCHOR=30
```

The Issue Collector and Root Cause Analyzer each have an independent budget of 40 turns. The Rule Generator has an independent 30-turn parent budget, and every per-anchor Synthesizer run has its own independent 30-turn limit. Delegated Synthesizer turns do not consume the waiting Rule Generator's budget in either runtime. At most five anchor runs execute concurrently, and one synthesis request contains at most eight intents.

Both runtimes treat these model-turn ceilings as request limits rather than a dollar budget. VAMINER does not calculate, collect, or report monetary cost estimates. Provider-reported cost fields are ignored; only request and token usage are retained.

#### Proxy access

In network environments where DuckDuckGo cannot be reached directly, including some connections from mainland China, configure the standard proxy variables in the repository-root `.env`:

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

No additional proxy configuration is required. VAMINER loads these variables before constructing its HTTP clients and bridges the HTTPS proxy (falling back to the HTTP proxy) to the local web-search client. `NO_PROXY` keeps loopback traffic direct. These variables are process-wide, so model-provider, GitHub, web-search, and web-fetch requests may all use the configured proxy.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to enable optional tracing. `LANGFUSE_BASE_URL` is needed only for a custom Langfuse server; otherwise the SDK default is used. General Miner limits live in `src/miner/utils/config.py`; Pydantic-specific model and compaction settings live in `src/miner/runtimes/pydantic/config.py`.

One mining input produces one trace named `VAS-XXXX Miner @<runtime>`, whose root input is the original typed mining input and whose root output is the final saved VAS rule. Pydantic AI contributes its native `invoke_agent` → `chat`/`execute_tool` OpenTelemetry spans. The official Claude plugin contributes Conversational Turn → Generation/Tool observations beneath VAMINER-owned phase spans; the cross-process synthesis orchestration span remains application-owned so child Claude runs can inherit live W3C context. Rich Hook/stream events are console and run-file diagnostics only and do not create duplicate Langfuse observations.

Both Runtime Adapters enforce the same Phase Authority. RCA reads source through typed list/search/read operations and writes only valid top-level Case Artifacts. Rule Generation reads only Case Artifacts and invokes synthesis. Synthesizers read scoped evidence and run ast-grep without write, network, shell, or delegation tools. RCA cleanup is explicit before pure validation; cache loading and final VAS validation never mutate the filesystem.

### Tests

Run the focused behavior suite with:

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

An empty `query` is the disabled-anchor sentinel. Disabled anchors remain in the VAS so their intended inspection behavior is visible, but they are never executed and add no ranking weight. Enabled anchors remain strictly validated. When disabled anchors exist, collective coverage gaps are published as warnings instead of blocking the degraded VAS.

## Anchor Quality

A complete anchor set is recall-oriented and behavior-distinct:

- Every enabled anchor matches at least one generated case and, for issue inputs, an RCA-declared site in the buggy repository.
- When no anchor is disabled, the enabled set covers every generated case and every required example-suite source span.
- Each anchor represents one distinct observable behavior in the causal chain, even when case coverage overlaps.
- `behavior` describes only the local operation matched by that anchor; cross-site relationships, exploit conditions, and review questions belong in `inspect_hint`.
- Each per-anchor query is centered on its target `behavior`. To reduce overlap with sibling anchors, one precision pass may add local defect-relevant structure supported by every required case and the RCA site, such as requiring the target operation to appear inside an `if` statement. Project-specific or full-chain constraints remain out of bounds.
- A Synthesizer normally returns an empty `plan_suggestion`. It may briefly suggest deleting, merging, or revising intents only when observed repository precision is materially poor and required-case recall can be preserved; the Rule Generator decides whether one bounded plan refinement is worthwhile.
- Generic calls, assignments, definitions, and conditions are rejected unless structural or API constraints make them rule-sensitive.
- Precision refinement is for reducing overlap between sibling anchors. Unrelated repository matches do not justify narrowing the query; keep recall and lower `query_weight` when needed.

See [the vas-scanner skill](src/.vaminer/skills/vas-scanner/SKILL.md) for its internal rule-execution and reporting workflow.
