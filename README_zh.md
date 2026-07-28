# VAMINER

[English](README.md)

VAMINER 将一个已报告的软件问题转换为变体分析规范（Variant Analysis Specification，VAS），并把生成的规则与 Agent Skill 一起打包，用于在较新版本或其他代码仓库中查找相关缺陷。

系统采用 **宽召回搜索、精确分析（sound search, precise analysis）** 模式：

- 使用确定性的 ast-grep 锚点查找与缺陷相关的代码热点并排序。
- 由分析 Agent 判断候选代码是否违反规则中的行为场景。

锚点只用于检索和导航，本身不代表代码中一定存在缺陷。

## 项目范围

本仓库负责生成 VAS 规则，**不提供**面向最终用户的独立规则 Runner。如果要在其他项目中扫描潜在的 1-day 缺陷或漏洞，需要把仓库自带的 [`vas-scanner`](src/.vaminer/skills/vas-scanner/) Skill 安装到编程 Agent 中，然后让 Agent 使用生成的规则执行扫描。

## 快速开始

### 环境要求

- Python 3.12 或更高版本
- `uv`
- Git
- `PATH` 中可用的 `ast-grep` 或 `sg` 命令
- 支持的 LLM 服务及对应 API Key

### 安装与配置

```bash
git clone <本仓库地址>
cd vaminer
uv sync
cp .env.example .env
```

示例配置默认使用 DeepSeek。请在仓库根目录的 `.env` 中填写 `DEEPSEEK_API_KEY`；如需使用其他服务，请参考 [LLM 配置](#llm-配置)。

开始生成规则前，请确认 ast-grep 已正确安装：

```bash
ast-grep --version
```

### 生成规则

向 Miner 传入 CVE ID 或 GitHub Issue URL：

```bash
uv run python -m src.miner.main CVE-2024-XXXX
```

```bash
uv run python -m src.miner.main https://github.com/owner/repository/issues/123
```

如果需要复用上一次执行中仍然有效的结果，可添加 `--use-cache`：

```bash
uv run python -m src.miner.main --use-cache CVE-2024-XXXX
```

最终规则会写入：

```text
src/.vaminer/skills/vas-scanner/rules/VAS-XXXX.json
```

分析结果、测试用例、缓存和锚点报告保存在 `../vas_ws/miner/VAS-XXXX/` 下，运行日志保存在 `logs/` 下。

## 在其他项目中运行生成的规则

生成的 JSON 规则需要由 Agent 通过 `vas-scanner` Skill 执行；本仓库中没有需要单独启动的 Runner。

1. 将完整的 Skill 目录复制到目标项目中 Agent 能够识别的 Skill 目录。例如：

   ```bash
   mkdir -p /path/to/target-project/.agent/skills
   cp -R src/.vaminer/skills/vas-scanner \
     /path/to/target-project/.agent/skills/
   ```

   必须复制整个目录，而不只是 `SKILL.md`。该目录包含扫描脚本、分析规范，以及 `rules/` 子目录中的所有已生成规则。如果安装 Skill 后又生成了新规则，请把新的 `VAS-XXXX.json` 复制到已安装 Skill 的 `rules/` 目录，或重新安装整个 Skill。

2. 确认编程 Agent 的 `PATH` 中可以找到 `ast-grep` 或 `sg`。

3. 使用 Agent 打开目标项目，并指定要执行的规则：

   ```text
   /vas_scanner Run VAS-XXXX rule on this project and find potential defects/vulnerabilities.
   ```

   将 `VAS-XXXX` 替换为实际生成的规则 ID。如果所使用的 Agent 采用其他 Skill 目录或调用语法，请使用对应方式，但要保持 `vas-scanner` 目录内容完整。

该 Skill 会使用规则中的 ast-grep 锚点确定性地查找并排序候选文件，再让 Agent 根据规则场景逐个分析候选文件。最终的 `report.json` 会写入：

```text
<target-project>/.vas/VAS-XXXX/run_<timestamp>/report.json
```

## 规则生成流程

Miner 按照以下确定性顺序执行：

1. **问题收集（Issue Collection）**：收集问题描述、仓库来源以及有缺陷和已修复的 Commit。
2. **根因分析（Root Cause Analysis）**：确定具体缺陷行为和修复模式，并提取最小原始用例及其变体。
3. **规则生成（Rule Generation）**：生成规则摘要、相互独立的不安全/安全场景，并为因果链中每个不同的规则敏感位置生成不含查询语法的锚点意图。
4. **AST-Grep 合成（AST-Grep Synthesis）**：在相互隔离且有界的 Synthesizer 上下文中逐个处理意图，然后按确定性顺序聚合锚点批次，并在全部用例和有缺陷的仓库版本上重新扫描验证。
5. **组装（Assembly）**：以原子方式接受验证后的锚点并输出 VAS 规则。
6. **生成后锚点报告（Post-generation Anchor Report）**：独立生成用例覆盖和仓库热点报告。

Rule Generator 不加载 ast-grep Skill，也不修改已验证的锚点。AST-Grep Synthesizer 独占查询语法、查询与行为的一致性以及查询验证。

Agent 运行时基于 Pydantic AI 和兼容 OpenAI Chat Completions 的接口。三个主要 Agent 是 Issue Collector、Root Cause Analyzer 和 Rule Generator；Rule Generator 会把类型化合成请求拆成每个意图一次 AST-Grep Synthesizer 运行。每次运行只看到自己的不可变意图，并拥有独立的依赖上下文和探索预算。确定性编排会恢复请求顺序，通过全新扫描生成精确用例覆盖和规范化仓库证据，再以原子方式接受整个批次。

### Miner 模块职责

- `src/miner/main.py`：编排流水线阶段、缓存接收、结果组装和持久化。
- `src/miner/core/agents.py`：构建 Agent 并连接 Synthesizer 的委托流程。
- `src/miner/core/capabilities.py`：定义可复用的 Capability 组合与本地 Skill 加载。
- `src/miner/core/context.py`：保存父工作流状态以及相互隔离的逐锚点运行状态。
- `src/miner/core/validation.py`：定义实时输出和缓存输出共用的确定性验收检查。
- `src/miner/anchors/review.py`：生成规则后的锚点审查报告，不负责流水线验证。
- `src/miner/tools/`：包含外部证据、代码仓库和 Pydantic 输出适配器。
- `src/miner/anchors/scanner.py`：直接从可部署的 `vas-scanner` Skill 加载扫描引擎。

### LLM 配置

所有 Agent 复用由 `get_llm()` 返回的进程级 Pydantic AI 模型。

原生 DeepSeek：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=...
```

OpenAI 官方接口：

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.2
OPENAI_API_KEY=...
```

兼容 OpenAI Chat Completions 的接口：

```dotenv
LLM_PROVIDER=openai-compatible
LLM_MODEL=your-model
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://your-endpoint/v1
```

只有使用兼容接口时才需要 `OPENAI_BASE_URL`。`LLM_PROVIDER` 和 `LLM_MODEL` 是必填项。

外部证据和可选链路追踪也通过仓库根目录中不会提交到 Git 的 `.env` 配置：

```dotenv
GITHUB_TOKEN=...

LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
```

`GITHUB_TOKEN` 是可选项，用于认证 GitHub API 请求并提高请求限额。

Issue Collector 的延迟加载 `web-search` 和 `web-fetch` Capability 使用本地后备实现，不需要搜索服务 API Key。专用的 CVE 和 GitHub 工具仍然是首选证据来源。

#### Agent 请求预算

每次 Agent 运行默认最多允许 100 次模型请求。如需调整，请在仓库根目录的 `.env` 中设置：

```dotenv
MINER_MAX_REQUESTS_PER_AGENT=100
```

Issue Collector 和 Root Cause Analyzer 各自拥有独立的请求预算。Rule Generator 会汇总其子运行的用量，并在恢复执行后继续受这一总体预算约束。每个逐锚点 Synthesizer 运行还受代码内置的独立上限约束：32 次模型请求和 64 次工具调用。最多并行运行 4 个锚点，一次合成请求最多包含 8 个意图。

#### 代理访问

在 DuckDuckGo 无法直接访问的网络环境中（包括中国大陆的部分网络），请在仓库根目录的 `.env` 中配置标准代理变量：

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

不需要其他代理配置。VAMINER 会在创建 HTTP Client 前加载这些变量，并把 HTTPS 代理（未设置时回退到 HTTP 代理）传递给本地网页搜索 Client。`NO_PROXY` 使本机回环地址保持直连。这些变量对整个进程生效，因此模型服务、GitHub、网页搜索和网页抓取请求都可能使用所配置的代理。

如需启用可选的 Langfuse 链路追踪，请同时设置 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。只有使用自定义 Langfuse 服务时才需要设置 `LANGFUSE_BASE_URL`，否则使用 SDK 默认地址。文件读取、工具输出溢出、上下文压缩和 ast-grep 执行等其他 Miner 行为限制由 `src/miner/configs.py` 中的常量控制，不使用环境变量。

### 测试

运行行为测试和端到端测试：

```bash
uv run pytest
```

## 规则语义

- `summary` 是一条通用、规范性的软件安全要求。
- `scenarios.unsafe` 中的每一项都是独立、完整、由原始问题推导出的缺陷场景。
- `scenarios.safe` 中的每一项都是独立、完整、能够排除该缺陷的场景，并且优先于表面上的不安全匹配。
- `anchors` 只匹配缺陷行为中对规则敏感的热点操作，修复行为不作为锚点。

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

`behavior_weight` 表示目标检查行为在规则中的重要程度。为了保留召回率，实际查询有时只是较弱或更宽泛的近似，此时 `query_weight` 可以更低，并且排序时只使用 `query_weight`。文件优先级是不同已匹配锚点的查询权重之和；同一锚点重复匹配只会增加导航位置，不会重复增加分数。每个意图的 `required_cases` 只存在于合成请求中，不属于合成后的锚点或最终 VAS Schema。

## 锚点质量

有效的锚点集合需要完整召回，并且各锚点行为互不重复：

- 每个锚点都覆盖其要求的原始用例和转换变体。
- 每个锚点都匹配 RCA 在有缺陷代码仓库中声明的对应位置。
- 每个锚点代表因果链中一个不同且可观察的行为，即使不同锚点的用例覆盖发生重叠。
- 除非结构约束或 API 约束使其对规则敏感，否则拒绝使用泛化的调用、赋值、定义和条件作为锚点。
- 仓库匹配结果用于在保持召回率的前提下提高精度；额外匹配不能作为缩窄查询并丢失必要证据的理由。

关于 Skill 内部的规则执行和报告流程，请参阅 [`vas-scanner` Skill](src/.vaminer/skills/vas-scanner/SKILL.md)。
