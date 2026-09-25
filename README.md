# VAMINER

[中文说明](README_zh.md)

VAMINER turns a known software defect into a reusable rule for finding related defects in source code.

It supports two steps:

- **Offline rule generation** from a CVE, a GitHub issue, or a directory of related source examples.
- **Online code scanning** with the generated rule through the bundled `vas-scanner` Agent Skill.

## Installation

Requirements:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Git
- An API key for a supported LLM provider, or an authenticated Claude CLI installation

```bash
git clone <this-repository-url>
cd vaminer
uv sync
cp .env.example .env
```

Configure the model provider in `.env`. For example:

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=...
```

OpenAI and OpenAI-compatible endpoints are also supported:

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.2
OPENAI_API_KEY=...
```

For an OpenAI-compatible endpoint, also set `OPENAI_BASE_URL`.

```dotenv
LLM_PROVIDER=openai-compatible
LLM_MODEL=your-model
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://your-endpoint/v1
```

To use an authenticated Claude CLI instead of the SDK runtime, set:

```dotenv
MINER_AGENT_RUNTIME=claude-cli
CLAUDE_CODE_MODEL=claude-sonnet-4-6
```

If GitHub API rate limits apply to your network, you can optionally add a token:

```dotenv
GITHUB_TOKEN=...
```

## Generate a Rule

### From a CVE or GitHub Issue

```bash
uv run python -m src.miner.main --issue CVE-2024-XXXX
uv run python -m src.miner.main --issue https://github.com/owner/repository/issues/123
```

You can pass multiple references after `--issue`; VAMiner processes them one by one and generates a separate rule for each issue.

Issue inputs can be CVE IDs, GitHub Issue URLs, or another issue/report reference that the configured evidence sources can resolve.

### From an Example Suite

Use a directory containing source examples that represent the same defect pattern:

```bash
uv run python -m src.miner.main --example-suite /path/to/examples
```

The directory may contain nested subdirectories. Source files can be accompanied by filenames, comments, labels, or a manifest that distinguishes good and bad examples.

The generated rule is saved under:

```text
src/.vaminer/skills/vas-scanner/rules/VAS-XXXX.json
```

Add `--use-cache` to reuse valid results from an earlier run.

To remove a generated rule and its associated artifacts, run:

```bash
uv run python -m src.miner.main --delete VAS-XXXX
```

## Scan a Repository

Copy the complete `vas-scanner` directory into a skill directory recognized by your coding agent:

```bash
# codex/opencode/... lookup ".agents/skills", while claude code lookups ".claude/skills"
mkdir -p /path/to/target-project/.agents/skills
cp -R src/.vaminer/skills/vas-scanner \
  /path/to/target-project/.agents/skills/
```

Open the target repository with the coding agent and invoke the skill with the generated rule ID:

```text
/vas_scanner Run VAS-XXXX on this repository and report potential defects.
```

The report is written to:

```text
<target-project>/.vas/VAS-XXXX/run_<timestamp>/report.json
```

Keep `SKILL.md`, `scripts/`, and `rules/` together when copying the skill.

## More Information

- [`vas-scanner` skill](src/.vaminer/skills/vas-scanner/SKILL.md)
- [中文说明](README_zh.md)
