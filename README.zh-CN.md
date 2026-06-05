# openworkflow

*[English](README.md) | 简体中文*

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-125%20passing-brightgreen)
![Dependencies](https://img.shields.io/badge/core%20deps-0-success)
![Stars](https://img.shields.io/github/stars/chaizijun1/openworkflow?style=social)

> **Claude 隐藏的多智能体 `Workflow` 引擎——开源、纯 Python、随处可跑。**
> 用一段脚本扇出几十个子 agent,只把*最终答案*留进上下文。

**[⚡ 30 秒上手](#安装与运行)** · [在 Claude Code 中使用](#在-claude-code-中使用作为-workflow-工具) · [局限与风险](#局限与风险)

**Claude Code `Workflow` 工具的开源替代**——把这套多智能体编排引擎带到 Python,随处可用(也能作为 MCP 工具接回 Claude Code,而原生 `Workflow` 在大多数版本里是隐藏/不可用的)。

它复刻了执行模型:编排器编写一段脚本;脚本内发起的工具调用会**暂停**、执行、并把结果返回给**正在运行的脚本**——**而不是**返回到模型的上下文窗口——所以只有脚本的最终返回值会回到模型。这正是它能在不撑爆上下文的前提下,廉价地扇出(fan-out)几十个 agent 的关键。

> **关于本项目。** openworkflow 是对 Claude Code 中 `Workflow` 多智能体编排理念的独立开源实现——基于其公开可观察的接口(原语名称、输入形态、已记录的行为)构建。运行时、沙箱、RPC、后端与工具等实现**完全原创**,不打包任何第三方源码。"Claude" / "Claude Code" 为其各自所有者的商标。请自担风险使用;在依赖它之前请先阅读 **[局限与风险](#局限与风险)**。

---

## openworkflow 能给你什么

- **沙箱脚本里的六个编排原语**——`agent()`、`parallel()`(屏障扇出)、`pipeline()`(无屏障流水线)、`phase()`、`log()`、`workflow()`(内联子 workflow)——外加贯穿整次运行的共享、硬上限 **token `budget`**。
- **会用工具的子 agent**——`agent()` 跑真实多轮循环,覆盖 Read/Write/Edit/Bash/Grep/Glob/WebFetch/WebSearch/NotebookEdit **以及任意 MCP 服务器的工具**。
- **给 Claude Code 的 `Workflow` 工具**——自带 MCP server,任何支持 MCP 的 Claude Code 都能像原生(隐藏)那个一样调 `Workflow(...)`。
- **自主设计**——给一句中文任务,它替你编写、校验并运行编排脚本。
- **真实沙箱**——`--secure` 让不可信脚本在 OS 沙箱(macOS Seatbelt / Linux bwrap)下运行:无网络、scratch 外不可写,内核级强制。
- **断点续跑**——已完成的 `agent()` 调用记入 journal;崩溃后免费重放已完成的工作,带漂移检测。
- **零核心依赖**、可插拔 LLM 后端(mock / Anthropic API / 工具循环 / 客户端 sampling)、60 个测试。

```bash
pip install -e '.[anthropic]'
openworkflow do "比较三种缓存策略并给出推荐" --backend tool
```

→ **[30 秒上手](#安装与运行)**

---

## 工作原理

一个 workflow 就是一段 Python 脚本:首条语句是 `meta = {...}` 字面量,并定义 `async def main():`。在 `main` 内有六个原语可用。当脚本调用 `agent()`(或某个工具)时,该调用在脚本**之外**执行,只有它的*结果*流回正在运行的脚本——所以你可以扇出几十个子 agent,而只有最终返回值进入模型的上下文窗口。

**脚本契约**
- 首条语句是 `meta = {"name", "description", "phases"}` 字面量。
- 禁用 clock/RNG:`time.*`、`datetime.now/utcnow`、`random/secrets/uuid` 在校验期被拒绝。这保证脚本确定性,从而崩溃后能重放已完成的工作(resume)而不是重花 token。

**六个作用域内的原语**

| 原语 | 语义 |
|---|---|
| `agent(prompt, opts?)` | 派生一个子 agent。无 `schema` 返回文本;给定 JSON `schema` 时强制其调用 `StructuredOutput` 工具并返回校验后的对象。用户跳过则返回 `null`。`opts`:`label`、`phase`、`schema`、`model`、`isolation:'worktree'`、`agentType`。 |
| `parallel(thunks)` | **屏障**:等待全部完成。抛错的 thunk 解析为 `null`(调用本身不会 reject)→ 用 `.filter(Boolean)`。 |
| `pipeline(items, ...stages)` | 阶段间**无屏障**:A 可处于阶段 3 而 B 仍在阶段 1。多阶段工作的默认选择。每个 stage 收到 `(prevResult, originalItem, index)`。 |
| `phase(title)` | 开启一个进度分组;之后的 `agent()` 归入其下。 |
| `log(message)` | 进度树上方的旁白行。 |
| `workflow(nameOrRef, args?)` | 内联运行另一个 workflow;共享本次运行的并发上限、agent 计数、中止信号与 token 预算。**仅一层嵌套**。 |
| `args` / `budget` | 输入值 / 共享 token 上限。`budget = {total, spent(), remaining()}`;`total` 是**硬上限**——一旦 `spent() ≥ total`,后续 `agent()` 抛错。池在整个运行及所有嵌套 workflow 间共享。 |

**workflow 存放位置**——已保存的脚本从这些位置发现:`~/.openworkflow/workflows/`(user)、`./.openworkflow/workflows/`(project)、内置 `workflows/` 目录,以及插件目录;优先级 内置 < plugin < project < user。

通过 CLI、Python API,或 `Workflow` MCP 工具运行 workflow——见 [安装与运行](#安装与运行)。

---

## 安装与运行

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

### 本地 / 弱模型驱动(Qwen、llama.cpp、任意本地网关)

openworkflow 经过硬化,**可被本地弱模型驱动**(例如经 LiteLLM 网关的 27B Qwen,即 xclaude 场景),不止服务前沿模型。用环境变量把后端指向你的网关即可,**无需任何代码补丁**:

```jsonc
// mcp.json —— 把 openworkflow 接到本地 Anthropic 兼容网关
{"mcpServers": {"openworkflow": {
  "command": "openworkflow-mcp",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",   // 你的网关
    "ANTHROPIC_API_KEY":  "网关接受的任意 token",
    "OPENWORKFLOW_BACKEND": "tool"
  }
}}}
```

弱模型很难一次写对多行 `script`。驱动工具有三条路,**由易到难**——优先前两条:

1. **`task=`** —— 给一句自然语言,服务端替你设计 + 校验 + 运行(模型完全不写 Python):
   `Workflow(task="并行回答3个常识问题再汇总")`。
2. **`name=`** —— 直接调已写好的可靠 workflow:`Workflow(name="code-review", args=["a.py","b.py"])`。
   `WorkflowList` 可列出(内置:`code-review`、`vote`、`deep-research`、`bug-hunt`)。
3. **`script=`** —— 裸 Python 源码,留给确实能写脚本的模型。

**内置容错**(默认开)让被弄坏的 `script` 被还原而非拒绝——`sandbox.normalize_script` 会剥掉 markdown ` ```python ` 围栏、JSON 编码、单/双/三引号包裹(真换行或转义换行均可)、前导散文,且支持任意组合,再用 `ast.parse` 校验。缺省参数传成字符串 `"null"` 会被归一成 `None`;源码误塞进 `name`/`scriptPath` 会被重路由到 `script`。校验确实失败时,错误里附**最小正确范例**,便于模型自我纠正。

容错由 **`OPENWORKFLOW_LENIENT`**(默认 `1`)控制。设 `OPENWORKFLOW_LENIENT=0` 恢复严格原生契约(不解包、不归一)——便于和原版做行为对齐测试。无论开关如何,强模型给的干净脚本都不会被改动。

集成配方、覆盖的失效模式清单与验收基线见 [`LOCAL_LLM_HARDENING.md`](LOCAL_LLM_HARDENING.md)。

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
