# openworkflow · 本地/弱模型硬化指南(Local-LLM Hardening Guide)

> **这份文档写给「在 openworkflow 仓库里工作的 agent 会话」。**
> 目标:把「让 openworkflow 在 xclaude(rebrand 的 Claude Code + 本地 Qwen)里完整可用」所需的容错与适配,**逐步内化进 openworkflow 本体**,最终 **无需任何外部补丁** 即可直接在 xc / 任意本地弱模型下实现完整功能。
>
> 工作方式:读完本文 → 按「§4 硬化路线图」逐项实现 → 每项都补 fixtures/tests → 用「§5 验收」确认 → 更新 README 的本地章节。每完成一项就在本文 checklist 打勾并提交。

---

## 1. 背景:xc 怎么连 openworkflow(集成配方,零代码改动)

- **xc** = rebrand 的 Claude Code `2.1.112`(最后一个含 JS bundle 的版本),后端经 **LiteLLM 网关**(暴露 Anthropic `/v1/messages`,`127.0.0.1:4000`)→ llama.cpp **Qwen3.6-27B**。
- openworkflow 作为 **MCP server** 接回 xc,给它 `Workflow` / `WorkflowList` 两个工具。
- **连接全靠环境变量,无需改 openworkflow 代码**:`AnthropicBackend` / `ToolAgentBackend` 都用 `anthropic.AsyncAnthropic()`(默认读 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`)。xc 的 `mcp.json` 里这样配:

```json
{
  "mcpServers": {
    "openworkflow": {
      "command": "/path/to/owf-venv/bin/openworkflow-mcp",
      "args": [],
      "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
        "ANTHROPIC_API_KEY": "any-token-the-gateway-accepts",
        "OPENWORKFLOW_BACKEND": "tool"
      }
    }
  }
}
```

- 网关 `model_name: "*"` 兜底 → backend 默认模型名(`claude-sonnet-4-6` 等)随便,全部路由到本地 Qwen。
- **xc 侧两个必做项(不在 openworkflow 仓库,但实现者要知道)**:
  1. `settings.json` 的 `permissions.allow` 必须含 `mcp__openworkflow__Workflow`、`mcp__openworkflow__WorkflowList`(dontAsk 模式下不放行 MCP 工具会被直接拒)。
  2. 系统提示里给一句 Workflow 用法提示(见 §3 的失效模式,提示能降低弱模型试错次数)。

---

## 2. 核心矛盾:为什么弱模型驱动 Workflow 会失败

`Workflow` 工具的契约(对标原生)是:模型传 `script`(**原始 Python 源码**,首句必须是 `meta = {...}` 字面量),或传 `name`(已存 workflow),或 `scriptPath`。

**强模型**能精确产出原始多行源码。**本地 27B 等弱模型**对「多行字符串工具参数」有系统性毛病,导致脚本进不了校验。**这不是模型不会编排**——实测 Qwen 写出的脚本逻辑是对的(`meta=...; async def main(): parallel([...])`),栽在**传参格式**上。

实测(xc + Qwen,同一任务)硬化前后:**37 次调用全失败 → 3 次调用成功扇出**。差距全在容错。

---

## 3. 已知弱模型失效模式 + 当前缓解(已在 `openworkflow/mcp_server.py`)

| # | 失效模式 | 表现 | 状态 |
|---|---|---|---|
| F1 | **script 被多套一层引号 / JSON 编码 / md 围栏 / 前导散文** | `script` 首字符是 `"`/`'`/` ``` `,或 `meta` 前有散文 → 校验报 `the FIRST statement must be a meta = {...}` | ✅ **已内化**(P1):`sandbox.normalize_script` 覆盖全部形态及组合,有 11 fixtures + 22 用例 |
| F2 | **缺省参数传成字符串 `"null"`** | `script="null"` 且 `scriptPath="null"` → 报 `provide one of script/name/scriptPath` | ✅ **已内化**(P1):`mcp_server._nullish`,纳入测试,挂 `OPENWORKFLOW_LENIENT` |

F1/F2 已从「外部补丁」升级为仓库内一等公民(默认开、有测试、可经 `OPENWORKFLOW_LENIENT=0` 关回严格)。**已无独立的外部补丁**;§6 仅留作历史对照。

> ⚠️ 这只是「止血」。要达到「无需外部补丁」,需把容错做成 openworkflow 的**一等公民**(默认开启、有测试、文档化),并补齐下面路线图中尚未覆盖的失效模式。

---

## 4. 硬化路线图(逐项内化,消除外部补丁)

> 每项格式:**问题 → 做法 → 验收测试**。完成后在 `[ ]` 打勾并 commit。建议新建 `tests/test_weak_model_inputs.py` 收集所有 fixtures。

- [x] **P1 · script 摄取彻底硬化**(把 `_unwrap_script` 升级为「尽力提取从 `meta` 开始的合法 Python」)✅
  - 问题:除 F1 的引号/JSON,弱模型还会产出:① markdown 代码围栏 ` ```python … ``` `;② `meta` 之前有前导散文/注释/空行;③ 整段是合法 JSON 字符串(`"\n"` 转义);④ 单引号包裹;⑤ 把内联源码误塞进 `scriptPath`/`name`。
  - 做法:实现一个 `normalize_script(raw) -> str`:依次尝试 (a) 去 markdown 围栏;(b) `json.loads`(若是字符串);(c) 剥成对首尾引号;(d) 从第一处出现 `^\s*meta\s*=` 起截取到末尾;(e) `ast.parse` 验证可解析。任一步得到「首句 `meta=` 且能 parse」即返回。把它用在 `script`、以及当 `name`/`scriptPath` 的值其实是源码(含换行/`meta=`)时也兜底转走。
  - 验收:`tests/fixtures/weak_inputs/` 放 ≥8 个真实坏样本,`normalize_script` 全部产出可被 `compile_script` 接受的源码。
  - **已实现**(`openworkflow/sandbox.py::normalize_script` + `_is_workflow_source` + 5 个 peel;`mcp_server._sanitize_args` 调用):覆盖①–⑤及其**任意组合**(BFS peel,最少剥离优先);结构校验=`ast.parse` 且首句 `meta = {dict}`;干净脚本原样返回,无法还原则原样返回让严格校验照常报错。挂 `OPENWORKFLOW_LENIENT`(默认开,`=0` 恢复严格)。11 个 fixtures + 22 用例(`tests/test_weak_model_inputs.py`),全套 60→82 绿。

- [x] **P2 · 错误信息「教模型改」**(弱模型靠 error 反馈重试)✅
  - 问题:当前 error 只说「错在哪」,不给「怎么对」。弱模型据此改不动。
  - 做法:校验失败时,error 里附**一个最小正确范例**(3-5 行的 `meta=...; async def main(): return await parallel([...])`)+ 一句「pass raw Python source, do NOT wrap in quotes」。
  - 验收:坏输入返回的字符串里含 `meta = {` 范例与「raw source」字样。
  - **已实现**(`sandbox.py::SCRIPT_FORMAT_HINT` + `MINIMAL_EXAMPLE`):空脚本 / 首句非 `meta` / `SyntaxError` 三类格式错误都附最小范例与「raw source」提示;经 `execute_workflow` 返回给模型的 `error:` 字符串同样带上。4 用例覆盖(单元 + 端到端)。

- [x] **P3 · 给 Workflow 工具加可选 `task` 参数(根本招:免模型写脚本)**✅
  - 问题:让弱模型现写编排脚本是最大瓶颈。
  - 做法:`Workflow(task="一句话任务")` → 服务端用 backend 调 `author.py` 的自动设计(对标 CLI 的 `do`/`design`)生成并校验脚本再跑。这样弱模型只需给自然语言,完全绕开「写 Python」。注意:authoring 也走同一个本地 backend,质量受限 → 仍建议同时保留 §P4。
  - 验收:`Workflow(task="并行回答3个常识问题再汇总")` 在本地后端下能跑通并返回结果。
  - **已实现**(`mcp_server.execute_workflow` 新增 `task=` + `Workflow` 工具新增 `task` 入参,经 `runtime.do_task` 设计→校验→运行;footer 标注 `auto-designed '<name>' by <scaffold|llm>`)。优先级:`script`/`name`/`scriptPath` > `task`(显式脚本永远优先);`task` 也走 `_nullish`。工具描述改为「EASIEST: task / 然后 name / ADVANCED: script」引导弱模型走 `task`。4 用例覆盖。

- [x] **P4 · 命名 workflow 优先 + 内置一批通用模板**✅
  - 问题:复杂编排弱模型写不对;最可靠是「按名调已写好的」。
  - 做法:`workflows/` 内置几个高频可靠模板(如:并行审查一个目录的代码并汇总、一个主题多角度调研汇总、N 路投票取多数)。强化 `WorkflowList` 的描述,让模型优先 `name=` 调用。
  - 验收:`WorkflowList` 列出内置模板;`Workflow(name="code-review", args={...})` 跑通。
  - **已实现**:新增 `workflows/code_review.py`(`code-review`,按文件并行审查→汇总)、`workflows/vote.py`(`vote`,N 路投票取多数);多角度调研沿用既有 `deep-research`,逐文件流水沿用 `bug-hunt`。两个新模板的 `args` 容错(list / dict / str / None 都能跑)。`list_workflows()` 与 `WorkflowList` 描述加了「优先 `Workflow(name=...)` 而非手写脚本」的引导。10 用例(注册、编译、跨 args 形态运行)。

- [ ] **P5 · lenient 模式默认开 + 开关**
  - 做法:把 P1/P2 的容错挂在 `OPENWORKFLOW_LENIENT`(**默认 1**;设 0 可关回严格契约)。保证「开箱即用对弱模型友好」。
  - 验收:无任何 env 时弱样本即被救活;`OPENWORKFLOW_LENIENT=0` 时恢复严格报错。

- [ ] **P6 · 回归测试 + CI**
  - 做法:所有弱样本进 `tests/`,纳入现有 60 测试套件;保证未来重构不回归。
  - 验收:`pytest` 全绿,弱样本用例计入。

- [ ] **P7 · 文档回写**
  - 做法:每内化一项,更新 `README.md` / `README.zh-CN.md` 的「Local / weak-model」小节;并在本文 checklist 打勾、删去对应的「外部补丁」描述。

---

## 5. 验收标准(达到即「无需任何外部补丁」)

一个**全新克隆**的 openworkflow,仅靠 env 指向本地网关(无任何手工 patch),在 xc 中满足:

1. **简单扇出**:模型现写脚本 **或** 用 `task=` 参数,都能成功跑出多 agent 并行结果;
2. **坏输入容错**:`tests/fixtures/weak_inputs/` 全部被自动救活(引号包裹、JSON 编码、md 围栏、前导散文、`"null"` 等);
3. **命名 workflow**:`WorkflowList` 可列、`name=` 可调内置模板;
4. **零外部补丁**:容错全部在仓库内(`mcp_server.py` / `sandbox.py` / `author.py`),xc 侧只剩**配置**(mcp.json env + settings allow),无代码注入。

满足 1–4 即可删除本文「外部补丁」相关内容,本文转为纯「集成 + 设计说明」。

---

## 6. 当前外部补丁的精确内容(便于 review / 上游合并)

位置:`openworkflow/mcp_server.py`,`execute_workflow()` 入口前插入的三个 helper + 一行调用。**这是目前唯一的「补丁」,P1–P6 完成后应被其内化版本取代。**

```python
# --- tolerance for weaker models (e.g. local 27B) that mangle the tool args ---
def _nullish(v):
    """弱模型常把缺省参数传成字符串 "null"/"none"/""。"""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("null", "none", "undefined", "") else s


def _unwrap_script(s):
    """弱模型常把整段脚本再套一层引号 / JSON 编码 → 还原成裸 Python 源。"""
    s = s.strip()
    if not s or s[0] not in "\"'":
        return s
    try:
        d = json.loads(s)
        if isinstance(d, str) and d.lstrip().startswith("meta"):
            return d.strip()
    except Exception:
        pass
    q = s[0]
    body = s[1:]
    if body.rstrip().endswith(q):
        body = body.rstrip()[:-1]
    if body.lstrip().startswith("meta"):
        return body.strip()
    return s


def _sanitize_args(script, name, scriptPath):
    script, name, scriptPath = _nullish(script), _nullish(name), _nullish(scriptPath)
    if isinstance(script, str) and script:
        script = _unwrap_script(script)
    return script, name, scriptPath
```

调用点(`execute_workflow` 里,`_sampling_guardrails` 之后、`if script is not None:` 之前):

```python
    script, name, scriptPath = _sanitize_args(script, name, scriptPath)
```

**注意**:P1 应把 `_unwrap_script` 升级为更全面的 `normalize_script`(覆盖 md 围栏、前导散文、`ast.parse` 校验),并默认经 `OPENWORKFLOW_LENIENT` 控制;届时本节代码即被替换。

---

## 7. 本地复现 / 自测(实现者验证用)

```bash
# 1) 引擎层(不经 xc,直接验证 openworkflow 在本地模型上扇出)
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000 ANTHROPIC_API_KEY=any OPENWORKFLOW_BACKEND=tool
openworkflow run examples/voting.py --args '"Is 113 prime?"' --backend tool   # 应返回多数票

# 2) 模拟弱模型坏输入(单测容错,不花 GPU)
python -c "from openworkflow.mcp_server import _sanitize_args as s; \
print(s('null',None,'null')); \
print(s('\"meta = {}\nasync def main():\n    return 1\"',None,None)[0][:10])"
# 期望:(None,None,None)  和  以 'meta' 开头

# 3) 端到端(经 xc):mcp.json 配 openworkflow + settings allow 放行 →
#    xclaude -p "用 Workflow 工具并行回答3个常识问题再汇总"  应真扇出并返回
```

---

## 8. 设计边界(写清楚,避免做无用功)

- **慢**:子 agent 与父会话共用同一本地模型(单 GPU 槽),扇出是「逻辑并行、物理排队」,一次简单 3-agent ~分钟级。这是部署形态决定的,不是 bug;`parallel()` 仍有价值(省上下文、结构化),但别期待线性加速。
- **复杂脚本**:即便容错完美,弱模型写**大型**编排脚本仍可能逻辑出错 → §P3(`task=` 自动 author)与 §P4(命名模板)是绕开这点的正道。
- **不要**为了迁就弱模型而破坏强模型路径或原生契约语义:容错只在「无歧义可还原」时介入,`OPENWORKFLOW_LENIENT=0` 必须能恢复严格行为。

---

*维护:每完成一项 §4 路线图,更新本文 checklist 与 §6;全部完成后,本文从「硬化指南」降级为「本地集成说明」。*
