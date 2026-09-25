# VAMINER

[English](README.md)

VAMINER 会把一个已知的软件缺陷转换成可复用的规则，用于在源码中查找相同或相近的缺陷。

它支持两个步骤：

- **离线生成规则**：输入 CVE、GitHub Issue，或包含相关源码示例的目录。
- **在线代码扫描**：通过仓库自带的 `vas-scanner` Agent Skill，使用生成的规则扫描代码。

## 安装

环境要求：

- Python 3.12 或更高版本
- [`uv`](https://docs.astral.sh/uv/)
- Git
- 支持的 LLM 服务及 API Key，或已完成认证的 Claude CLI

```bash
git clone <本仓库地址>
cd vaminer
uv sync
cp .env.example .env
```

在 `.env` 中配置模型服务。例如：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=...
```

也支持 OpenAI 及兼容 OpenAI 接口的服务：

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.2
OPENAI_API_KEY=...
```

使用 OpenAI 兼容接口时，还需要设置 `OPENAI_BASE_URL`。

```dotenv
LLM_PROVIDER=openai-compatible
LLM_MODEL=your-model
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://your-endpoint/v1
```

如需使用已经完成认证的 Claude CLI，可改用以下配置：

```dotenv
MINER_AGENT_RUNTIME=claude-cli
CLAUDE_CODE_MODEL=claude-sonnet-4-6
```

如果需要提高 GitHub API 的请求限额，可以选填：

```dotenv
GITHUB_TOKEN=...
```

## 生成规则

### 从 CVE 或 GitHub Issue 生成

```bash
uv run python -m src.miner.main --issue CVE-2024-XXXX
uv run python -m src.miner.main --issue https://github.com/owner/repository/issues/123
```

如需批量处理，也可以在 `--issue` 后传入多个问题引用，VAMiner 会按顺序处理。

问题输入可以是 CVE ID、GitHub Issue URL，或能够由已配置证据源解析的其他问题/报告引用。

### 从示例目录生成

提供一个包含同类缺陷源码示例的目录：

```bash
uv run python -m src.miner.main --example-suite /path/to/examples
```

目录可以包含多层子目录。源码文件可以通过文件名、注释、标签或 manifest 区分正确和错误示例。如需批量处理，也可以在 `--example-suite` 后传入多个目录，VAMiner 会按顺序处理。

生成的规则保存在：

```text
src/.vaminer/skills/vas-scanner/rules/VAS-XXXX.json
```

添加 `--use-cache` 可以复用之前运行中仍然有效的结果。

删除生成的规则及其关联产物：

```bash
uv run python -m src.miner.main --delete VAS-XXXX
```

## 扫描代码仓库

将完整的 `vas-scanner` 目录复制到编程 Agent 能识别的 Skill 目录中：

```bash
# codex/opencode/... 配置于 ".agents/skills", 而 claude code 配置于 ".claude/skills"
mkdir -p /path/to/target-project/.agents/skills
cp -R src/.vaminer/skills/vas-scanner \
  /path/to/target-project/.agents/skills/
```

用编程 Agent 打开目标仓库，并使用生成的规则 ID 调用该 Skill：

```text
/vas_scanner Run VAS-XXXX on this repository and report potential defects.
```

扫描报告会写入：

```text
<target-project>/.vas/VAS-XXXX/run_<timestamp>/report.json
```

复制 Skill 时请保留 `SKILL.md`、`scripts/` 和 `rules/`，不要只复制规则 JSON 文件。

## 更多信息

- [`vas-scanner` Skill](src/.vaminer/skills/vas-scanner/SKILL.md)
- [English](README.md)
