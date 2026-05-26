# openworkflow

*[English](README.md) | 简体中文*

**Claude Code `Workflow` 工具的开源替代**——把这套多智能体编排引擎带到 Python,随处可用(也能作为 MCP 工具接回 Claude Code,而原生 `Workflow` 在大多数版本里是隐藏/不可用的)。

它复刻了执行模型:编排器编写一段脚本;脚本内发起的工具调用会**暂停**、执行、并把结果返回给**正在运行的脚本**——**而不是**返回到模型的上下文窗口——所以只有脚本的最终返回值会回到模型。这正是它能在不撑爆上下文的前提下,廉价地扇出(fan-out)几十个 agent 的关键。

> **关于本项目。** openworkflow 是对 Claude Code 中 `Workflow` 多智能体编排理念的独立开源实现——基于其公开可观察的接口(原语名称、输入形态、已记录的行为)构建。运行时、沙箱、RPC、后端与工具等实现**完全原创**,不打包任何第三方源码。"Claude" / "Claude Code" 为其各自所有者的商标。请自担风险使用;在依赖它之前请先阅读 **[局限与风险](#局限与风险)**。

---

## 1. Claude 的 Workflow 是什么

基于 Claude Code 2.1.150 公开可观察的接口(`Workflow` 工具确实存在,但**不出现在 `/help`** 中)。它暴露的形态如下:

**调用方式**(由主模型发起):
```js
Workflow({name: "deep-research", args: "<问题>"})   // 跑一个已保存的命名 workflow
Workflow({scriptPath: "<路径>"})                      // 重跑之前写好的脚本
```
每次调用都会把脚本持久化到 session 目录并返回路径,便于通过 `{scriptPath}` 迭代。

**脚本契约:**
- 首条语句必须是 `meta = { name, description, phases }` 字面量(纯字面量)。
- 脚本中**禁用 `Date.now()` / `new Date()`**:*"会破坏 resume"*。脚本必须确定性,以便崩溃后重放缓存结果(二进制中含一个带漂移检测的 REPL 重放引擎)。

**在作用域内的六个原语:**

| 原语 | 语义 |
|---|---|
| `agent(prompt, opts?)` | 派生一个子 agent。无 `schema` 返回文本;给定 JSON `schema` 时强制其调用 `StructuredOutput` 工具并返回校验后的对象。用户跳过则返回 `null`。`opts`:`label`、`phase`、`schema`、`model`、`isolation:'worktree'`、`agentType`。 |
| `parallel(thunks)` | **屏障**:等待全部完成。抛错的 thunk 解析为 `null`(调用本身不会 reject)→ 用 `.filter(Boolean)`。 |
| `pipeline(items, ...stages)` | 阶段间**无屏障**:A 可处于阶段 3 而 B 仍在阶段 1。多阶段工作的默认选择。每个 stage 收到 `(prevResult, originalItem, index)`。 |
| `phase(title)` | 开启一个进度分组;之后的 `agent()` 归入其下。 |
| `log(message)` | 进度树上方的旁白行。 |
| `workflow(nameOrRef, args?)` | 内联运行另一个 workflow;共享本次运行的并发上限、agent 计数、中止信号与 token 预算。**仅一层嵌套**。 |
| `args` / `budget` | 输入值 / 共享 token 上限。`budget = {total, spent(), remaining()}`;`total` 是**硬上限**——一旦 `spent() ≥ total`,后续 `agent()` 抛错。池在整个运行及所有嵌套 workflow 间共享。 |

**注册来源**(3 处,优先级 user > project > 内置):`~/.claude/workflows/*.js`、`.claude/workflows/*.js`、以及插件提供的 workflow。二进制中还定义了内置的 `workflow-subagent` agent 类型与一个 `/workflows` 视图。

---

## 2. 本项目如何映射到 Python

| 原版(JS / vm 沙箱) | 本项目(Python) |
|---|---|
| `export const meta = {...}` 为首语句 | `meta = {...}` 字面量为首语句(AST 校验) |
| 脚本体使用全局原语、async | `async def main():` 入口;原语注入到模块命名空间 |
| `Promise` / `await` / `Promise.all` | `asyncio` 协程 / `asyncio.gather` |
| `vm.Script` 沙箱(密封全局) | 两种模式:进程内 `compile()`+`exec()`(契约+确定性),或 **`--secure`**:脚本在 `sandbox-exec`/`bwrap` 下运行,原语经 RPC 管道——内核级强制,无网络、无文件外泄 |
| `Date.now`/`Math.random` 禁用 | 校验期拒绝 `time.*`、`datetime.now/utcnow`、`random/secrets/uuid` |
| journal + REPL 重放(漂移) | 追加式 JSONL journal,按 `sha256(phase\|label\|prompt)` 键;重放已完成的 `agent()` 结果,报告漂移 |
| `tools:["*"]` 的 workflow-subagent | `ToolAgentBackend`:真实多轮 tool-use 循环,覆盖 Read/Write/Edit/Bash/Grep/Glob/WebFetch/WebSearch/NotebookEdit + 任意 MCP 工具。另有 `AnthropicBackend`(单次文本/结构化)与 `MockBackend`(零成本)。 |
| MCP 服务器 / `mcp__*` 工具 | `MCPManager` 连接 `~/.openworkflow/mcp.json` 中的服务器(stdio + http/sse),按 `mcp__<server>__<tool>` 命名暴露其工具——一次集成,接通整个 MCP 生态 |
| `Workflow({...})` 工具(Claude Code 内) | `mcp_server.py` 把 openworkflow 暴露为 MCP `Workflow` 工具,任何支持 MCP 的 Claude Code 都能像原生(隐藏)那个一样调用——子 agent 经 API key 或经客户端 **sampling + 工具** 供能 |
| `opts.isolation:'worktree'` | 已实现——每个 agent 起一个 detached worktree,在其中运行,未改动则自动删除,有改动则保留并报告路径 |
| `opts.agentType`(自定义子 agent) | 解析为来自 `~/.openworkflow/agents/<type>.md` 的系统提示覆盖 |
| 插件 workflow(`<plugin>:<name>`) | 从 `~/.openworkflow/plugins/*/workflows/*.py` 扫描并命名空间化;优先级 内置 < plugin < project < user |

---

## 3. 安装与运行

```bash
cd openworkflow
pip install -e .                 # 核心(零依赖)
pip install -e '.[anthropic]'    # 使用真实 Anthropic 后端
pip install -e '.[mcp]'          # MCP 客户端 + Claude Code 桥接

# 列出已保存的 workflow
openworkflow list

# 按名运行已保存的 workflow(mock 后端 = 免费、确定性)
openworkflow run-name deep-research --args '"向量数据库的权衡?"' --backend mock

# 运行脚本文件,带硬 token 预算,输出 JSON
openworkflow run examples/voting.py --args '"113 是质数吗?"' --budget 300000 --json

# 真实 LLM:设置 key 后自动切换
export ANTHROPIC_API_KEY=sk-...
openworkflow run-name deep-research --args '"..."'            # 用 AnthropicBackend

# 让子 agent 真正读写文件、跑 shell(忠实的 subagent)
openworkflow run-name bug-hunt --args '["auth.py"]' --backend tool

# resume 崩溃的运行——已完成的 agent() 调用免费重放
openworkflow run-name bug-hunt --args '["a.py","b.py"]' --journal run.jsonl
openworkflow run-name bug-hunt --args '["a.py","b.py"]' --journal run.jsonl --resume
```

### 在 Claude Code 中使用(作为 `Workflow` 工具)

原生 `Workflow` 工具在大多数 Claude Code 版本中是隐藏/不可用的。openworkflow 自带一个 **MCP server**,为 Claude Code 提供一个输入形态相同的 `Workflow` 工具(`script` / `name` / `scriptPath`、`args`、`budget`)——任何支持 MCP 的 CC 版本都能用。

```bash
pip install -e '.[mcp,anthropic]'
claude mcp add openworkflow -- python3 -m openworkflow.mcp_server
# 或写进 .mcp.json:
# {"mcpServers": {"openworkflow": {"command": "python3", "args": ["-m", "openworkflow.mcp_server"]}}}
```

之后 Claude 就能像原版一样调用 `Workflow(script="…", args=…)`:它写一段编排脚本,server 在你机器上运行,**只有最终结果回到上下文**。

**子 agent 的模型从哪来?**(默认 `OPENWORKFLOW_BACKEND=auto`)
- **设了 `ANTHROPIC_API_KEY`** → `ToolAgentBackend`:子 agent 跑原生 tool_use 循环(Read/Write/Edit/Bash/Grep/Glob/Web/MCP)。能力最强,按 token 计费。
- **没有 key** → `auto` 回退到 `MockBackend`(安全空操作)。**不会**静默动用订阅。

⚠️ **sampling 与订阅风险。** 你可以显式开启 `OPENWORKFLOW_BACKEND=sampling`(纯文本)或 `sampling-tools`(文本 + 本地工具),把子 agent 的推理路由到**客户端自己的模型**——即用户的 Claude Code 订阅。这最贴近原版,但扇出型 workflow 会产生大量补全;在消费级订阅上这可能触发限流,并可能违反服务条款(以程序化方式放大消费级访问)。**因此它仅为显式 opt-in**;开启后我们会把并发钳为 1、应用保守的默认预算、并在 stderr 警告。**正式负载请用 API key。** 详见下方[局限与风险](#局限与风险)。

### 自主设计 workflow

像原版一样——主模型**自己编写编排脚本**——openworkflow 也能从一句任务自动设计 workflow。"大脑"(`author.py`)提示 LLM 生成符合契约的脚本,用运行时同一个 `compile_script` 校验,出错回喂重写;然后"身体"(运行时)执行。

```bash
openworkflow design "研究 GraphQL vs REST 的权衡"   # 只设计并打印脚本
openworkflow do "比较三种缓存策略并给出推荐" --budget 200000   # 设计并运行
```

无 key(mock)时用确定性的 *scaffold 设计器*,从任务线索选结构(并行扇出 + 综合,或逐项流水线),整条 设计→校验→运行 链路免费可跑。

---

## 局限与风险

在依赖本项目前请阅读。以下都是真实存在、且刻意挑明的问题。

### 安全
- **进程内模式(`exec`)不是安全边界。** 它只强制脚本契约 + 确定性。恶意脚本能做你 Python 进程能做的一切。**未加 `--secure` 时绝不要运行不可信脚本。**
- **`--secure` 是操作系统级,而非虚拟机级。** 它使用 macOS `sandbox-exec`(Seatbelt)或 Linux `bubblewrap`。它阻断网络出站与 scratch 外写入(内核强制,已有测试验证),但**不是**虚拟机/微 VM。注意:`sandbox-exec` 已被 Apple 官方标为*废弃*(仍可用,广泛使用);Linux 需安装 `bwrap`;**不支持 Windows**;默认允许读取(被挡的是出站),所以应理解为"无法外泄 / 无法在 scratch 外持久化",而非"什么都读不了"。
- **子 agent 的工具以你的权限在宿主上运行。** `ToolAgentBackend` 中的 `Bash`、`Write` 等以你的身份执行。可用 `ToolBox(confine=True)`、`allow_bash=False`,或对编排加 `--secure` 来限制。沙箱限制的是*脚本*,不一定限制工具型子 agent 在宿主上的每个动作。

### Claude 订阅 / 服务条款风险(sampling 后端)
- `OPENWORKFLOW_BACKEND=sampling` / `sampling-tools` 会把子 agent 调用路由到 **MCP 客户端的模型——即用户的 Claude Code 订阅**。扇出型 workflow 会产生大量补全;在消费级套餐上这可能**触发限流**,并可能**违反服务条款**(以程序化方式放大消费级访问)。**这带来账号风险,后果由使用者自负。**
- 已内置缓解:sampling **仅为 opt-in**(绝不静默默认——无 key 回退到 `mock`)、并发钳为 1、应用保守的 token 预算、并在 stderr 打印警告。**任何有量的场景请用 API key(`ToolAgentBackend`),这是受支持、按 token 计费、条款无歧义的正道。**
- 我们不是律师,以上不构成法律意见。使用 sampling 路径前请查阅 Anthropic 最新条款。

### 正确性 / 可靠性
- **sampling-tools 使用 ReAct 文本协议**,而非原生 tool_use。较弱的客户端模型可能输出非法 JSON 或跑偏;它是尽力而为,不如 API 工具循环可靠。
- **子 agent 质量 = 你后端的模型。** `MockBackend` 仅用于管路测试(回显,无推理)。真实结果需要 `anthropic`/`tool` 后端搭配有能力的模型。
- **resume 基于哈希**,非逐字节重放。确定性靠"脚本中禁用 clock/RNG"来保证,但经由工具结果引入的强非确定性仍可能导致漂移(会被报告,而非静默出错)。
- **脚本经 `exec()` 执行**,语法/逻辑错误会表现为 Python 异常(除 `meta`/确定性检查外),而非整洁的校验信息。

### 相对原版的范围缺口
- 工具集是核心集合(文件/shell/搜索/web/notebook)**加任意 MCP server**——不是 Anthropic 的全套内部工具。
- 无交互式 `/workflows` UI、无运行中"跳过此 agent"、无逐字节重放、无自动持久化到 session 目录。这些是体验/保真度,而非能力。

### 依赖 / 平台
- 核心**零依赖**。可选 extra:`anthropic`(真实 LLM)、`mcp`(MCP 客户端 + Claude Code server 桥接)。`WebSearch` 需要 `TAVILY_API_KEY` 或 `BRAVE_API_KEY`。
- `--secure` **仅支持 macOS 与 Linux**。其余功能跨平台。

---

## 使用建议(各场景该用什么)

| 你想要… | 用 |
|---|---|
| 编排 LLM + 文件/代码/shell 工作,要真实结果 | API key + `--backend tool`(`ToolAgentBackend`) |
| 安全运行**不可信 / 模型生成**的脚本 | 加 `--secure`(macOS/Linux) |
| 接入 Claude Code 作为 `Workflow` 工具,带预算 | MCP server + `ANTHROPIC_API_KEY` |
| 让子 agent 用 Claude Code 自己的模型 | `OPENWORKFLOW_BACKEND=sampling-tools` —— **仅在你接受上述订阅/ToS 风险时**,且只做小扇出 |
| 免费开发/测试编排逻辑 | `MockBackend`(`--backend mock`) |
| 访问数据库 / SaaS / GitHub 等 | 在 `~/.openworkflow/mcp.json` 配置 MCP 服务器 |

**经验法则:** 演示之外的一切**默认用 API key**;**sampling** 仅保留给微小、交互式、显式 opt-in 的用途;只要脚本不是你自己写的就打开 **`--secure`**;CI 与管路测试用 **`MockBackend`**。

---

## 测试

```bash
pip install pytest
python3 -m pytest tests/ -q
```

测试默认在零成本的 `MockBackend`/伪客户端上运行;`--secure` 的端到端用例在无可用 OS 沙箱时自动跳过。

---

## 许可证

MIT。详见 [LICENSE](LICENSE)。详见本文件顶部的"关于本项目"。
