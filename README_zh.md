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
- 使用 Pydantic AI 时，需要支持的 LLM 服务及对应 API Key
- 使用 Claude CLI 时，需要已安装 `claude` 命令并完成用户登录

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

模型工作区只包含源码和生成用例：

```text
../vas_ws/miner/VAS-XXXX/
├── src/
└── cases/
```

缓存和诊断产物位于模型工作区之外：

```text
output/
├── miner/VAS-XXXX/<input-id>/
│   ├── caches/                         # Issue Collection、RCA、Rule Generation 三种缓存
│   └── anchor_review.md
├── logs/miner/VAS-XXXX/<input-id>/
│   └── <trace-id>__<runtime>.log
└── artifacts/claude-code/<input-id>/<trace-id>/
    ├── issue-collection/
    ├── root-cause/
    └── rule-generation/
```

启用 Langfuse 时，`<trace-id>` 就是整个 workflow 的 Langfuse Trace ID；未启用时，VAMINER 会生成相同格式的本地 ID。可以通过 `VAMINER_OUTPUT_DIR` 或 `--output-dir` 整体调整 `output/` 的位置。

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
3. **规则生成（Rule Generation）**：生成规则摘要、相互独立的不安全/安全场景，并为因果链中每个不同、局部且规则敏感的位置生成不含查询语法的锚点意图。
4. **AST-Grep 合成（AST-Grep Synthesis）**：在相互隔离且有界的 Synthesizer 上下文中逐个处理意图。每次运行都会看到完整的只读锚点计划和一个目标 id，并为该目标返回一个结构化结果。
5. **组装与验证（Assembly and Validation）**：构建完整 VAS，然后由一个共享语义门在用例和源码根目录上扫描所有非空查询锚点。
6. **生成后锚点报告（Post-generation Anchor Report）**：独立生成用例覆盖和仓库热点报告。

Rule Generator 不加载 ast-grep Skill，也不编写查询文本。AST-Grep Synthesizer 独占查询语法以及查询与行为的一致性。如果无法生成可信查询，`query: ""` 会把该锚点标记为禁用；扫描和排序会跳过它，并在锚点审查文档和运行日志中突出显示。

运行时无关的任务可以由 Pydantic AI 适配器或隔离的 Claude Code CLI 适配器执行；每个阶段都显式路由，并且不会在运行时之间回退。共享合成层只构造逐 intent 子任务、通过注入的异步 executor 并发执行，并恢复请求顺序。Pydantic Rule Generator 把这些任务委派回 Pydantic AI Runtime；Claude Rule Generator 则把完整计划交给阶段专属 MCP 工具，由 MCP 宿主使用所选 model、effort、output format、限制和 JSON Schema，为每个 intent 启动一个独立 Claude CLI 任务。Claude 合成流程不会导入或调用 Pydantic AI adapter。

每个 Synthesizer 只获得相互独立的只读 source/cases 工具和类型化的 `run_ast_grep_query` 工具。Runner 只接受逻辑目标 `source` 或 `cases`，不暴露 Bash、命令字符串或任意文件系统路径。两个 Runtime 都不使用合成回执或逐子 Agent 语义门；语义验收统一由最终 `VASCoreInfo` 任务负责。

### Miner 模块职责

- `src/miner/agent/`：定义运行时无关的任务契约和阶段路由。
- `src/miner/models/`：保存问题、根因、锚点和 VAS 模型。
- `src/miner/mining/`：负责任务构建、工作流、示例套件接入和确定性验收。
- `src/miner/utils/`：负责通用配置、工作区布局、类型化缓存持久化、日志和遥测。
- `src/miner/tools/`：提供运行时无关的证据、仓库、用例、Skill 和 ast-grep 操作。
- `src/miner/runtimes/shared/`：包含运行时无关的异步任务委派和结果汇聚。
- `src/miner/runtimes/pydantic/`：包含 Pydantic AI Phase 适配器、Capability、LLM 构建、Hook 和工具适配器。
- `src/miner/runtimes/claude/`：包含 Claude CLI 实现、策略编译、子进程控制、协议解码、诊断产物和阶段专属 MCP 工具。
- `src/miner/anchors/`：负责生成规则扫描和生成后审查。
- `src/miner/main.py`：作为 CLI 组合入口，负责运行时选择、工作流执行、组装和持久化。

### LLM 配置

Pydantic AI 适配器保留现有的显式 provider 配置，并复用由 `get_llm()` 返回的进程级模型。

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

Claude CLI 适配器使用一套独立且刻意收窄的配置：

```dotenv
MINER_AGENT_RUNTIME=claude-code
CLAUDE_CODE_MODEL=claude-sonnet-4-6
```

也可以传入 `--claude-model claude-sonnet-4-6`。VAMINER 始终使用 `user` setting source 和显式模型名调用 Claude；鉴权与 provider 选择直接来自现有的 Claude CLI 用户会话。VAMINER 不接受 Claude API Key、Base URL、Bedrock/Vertex/Foundry 开关、自定义鉴权 settings 文件或其它 setting source。

外部证据和可选链路追踪也通过仓库根目录中不会提交到 Git 的 `.env` 配置：

```dotenv
GITHUB_TOKEN=...

LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
```

`GITHUB_TOKEN` 是可选项，用于认证 GitHub API 请求并提高请求限额。

Issue Collector 的延迟加载 `web-search` 和 `web-fetch` Capability 使用本地后备实现，不需要搜索服务 API Key。专用的 CVE 和 GitHub 工具仍然是首选证据来源。

#### Agent Turn 预算

每个 Phase 拥有独立的模型 Turn 预算。如需调整，请在仓库根目录的 `.env` 中设置：

```dotenv
MINER_MAX_TURNS_ISSUE_COLLECTION=40
MINER_MAX_TURNS_ROOT_CAUSE=40
MINER_MAX_TURNS_RULE_GENERATION=100
MINER_MAX_TURNS_PER_ANCHOR=30
```

Issue Collector 和 Root Cause Analyzer 各自拥有 40 Turns 的独立预算。Rule Generator 的父级预算为 100 Turns。每个逐锚点 Synthesizer 运行拥有独立的 30 Turns 上限。Pydantic 父 Agent 会把子运行用量合并到任务用量；Claude CLI 子运行保留各自的原生 Turn 计数，不计入正在等待的父 CLI Turn 计数。最多并行运行 5 个锚点，一次合成请求最多包含 8 个意图。

两个 Runtime 都将这些模型 Turn 上限作为请求次数限制，不设置美元预算。VAMiner 不计算、收集或报告金额成本估算；Provider 返回的费用字段会被忽略，只保留请求数和 Token 用量。

#### 代理访问

在 DuckDuckGo 无法直接访问的网络环境中（包括中国大陆的部分网络），请在仓库根目录的 `.env` 中配置标准代理变量：

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

不需要其他代理配置。VAMINER 会在创建 HTTP Client 前加载这些变量，并把 HTTPS 代理（未设置时回退到 HTTP 代理）传递给本地网页搜索 Client。`NO_PROXY` 使本机回环地址保持直连。这些变量对整个进程生效，因此模型服务、GitHub、网页搜索和网页抓取请求都可能使用所配置的代理。

如需启用可选的 Langfuse 链路追踪，请同时设置 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。只有使用自定义 Langfuse 服务时才需要设置 `LANGFUSE_BASE_URL`，否则使用 SDK 默认地址。通用 Miner 限制位于 `src/miner/utils/config.py`；Pydantic 专用的文件系统、工具输出溢出和上下文压缩限制位于 `src/miner/runtimes/pydantic/config.py`。

Langfuse 工作流 Trace 名称会包含实际路由的 Runtime，例如 `VAS-0001 Miner Workflow @pydantic-ai` 或 `VAS-0001 Miner Workflow @claude-code`；混合工作流会列出两个 Runtime ID。Claude Phase 采用与其他 Agent 运行一致的 observation 层级：一个 `<Agent> run` `AGENT` 下交错排列 `chat <model>` `GENERATION` 和每次完整工具执行对应的 `TOOL`。Chat 输入包含累计消息历史和可见工具目录；同一个 TOOL 同时保存参数和结果。终止协议事件只作为 Agent metadata，不再单独生成子 span。MCP 宿主内的 Synthesizer 会继承当前 Claude Agent 的 W3C trace context；合成 TOOL 会成为委派的 Claude CLI Agent 运行及其 model/tool 子节点的父节点。Claude Runtime 诊断会把子任务 attempt、工具活动、结果和用量追加到当前 VAS run log。事件负载会脱敏并限制大小。

Claude 文件权限按 Phase 隔离：Root Cause Analysis 可以读取单个 VAS 工作区，并且只能写入 `cases/` 顶层；Rule Generation 只能读取 `cases/`。Rule Generation 的源码 grounding 只由 MCP 宿主内的 Synthesizer 完成。原生工具拒绝会作为当前会话内的工具结果交给 Claude 自行处理，并仅保留为审计元数据；Runtime 外层修复循环只处理最终结构化输出失败，不承担工具重试。最终 RCA 校验会保留声明且符合规范的 case 文件，并删除之前 attempt 遗留的所有未声明文件。

### 测试

运行聚焦的行为测试：

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

空 `query` 是禁用锚点标记。禁用锚点仍保留在 VAS 中，以便展示预期检查行为，但永远不会执行，也不会增加排序权重。非空锚点仍接受严格验证；存在禁用锚点时，整体覆盖缺口会作为警告发布，而不会阻止降级 VAS。

## 锚点质量

完整的锚点集合以召回为目标，并且各锚点行为互不重复：

- 每个非空锚点至少匹配一个生成用例；对于问题输入，还必须匹配 RCA 在有缺陷代码仓库中声明的位置。
- 没有禁用锚点时，非空锚点集合必须覆盖全部生成用例以及示例套件要求的全部源码区间。
- 每个锚点代表因果链中一个不同且可观察的行为，即使不同锚点的用例覆盖发生重叠。
- `behavior` 只描述该锚点匹配的局部操作；跨位置关系、漏洞触发条件和检查问题属于 `inspect_hint`。
- 每个查询以目标 `behavior` 为语义核心。为了减少与兄弟锚点的重叠，可以在一次精度优化中加入所有必要用例和 RCA 位置都支持的局部缺陷相关结构，例如要求目标操作位于 `if` 语句内；仍禁止项目特有约束或完整因果链约束。
- Synthesizer 通常返回空的 `plan_suggestion`。只有在仓库匹配精度明显过差且能够保留必要用例召回时，才可简短建议删除、合并或调整 intent；是否进行一次有界的计划调整由 Rule Generator 决定。
- 除非结构约束或 API 约束使其对规则敏感，否则拒绝使用泛化的调用、赋值、定义和条件作为锚点。
- 精度优化用于减少兄弟锚点之间的重叠。无关的仓库额外匹配不能作为缩窄查询的理由；必要时应保留召回并降低 `query_weight`。

关于 Skill 内部的规则执行和报告流程，请参阅 [`vas-scanner` Skill](src/.vaminer/skills/vas-scanner/SKILL.md)。
