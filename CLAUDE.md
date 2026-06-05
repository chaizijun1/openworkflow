# CLAUDE.md — openworkflow

openworkflow 是 Claude Code `Workflow` 多 agent 编排引擎的开源 Python 复刻(`agent()/parallel()/pipeline()/phase()/log()/workflow()` 六原语 + sandbox + MCP server)。零核心依赖,`pip install -e '.[anthropic,mcp]'`,`pytest` 跑测试。

## 当前的重点工作:本地/弱模型硬化

本仓库正被接入 **xclaude**(rebrand 的 Claude Code + 本地 Qwen-27B,经 LiteLLM 网关)。目标是让 openworkflow 在**本地弱模型**下也能被完整驱动,**最终无需任何外部补丁**。

**若你被指派做这类工作(容错、弱模型适配、消除补丁),先读并按其执行:**

→ **[`LOCAL_LLM_HARDENING.md`](LOCAL_LLM_HARDENING.md)** ⭐

它包含:集成配方、已知弱模型失效模式、当前唯一外部补丁的精确内容(`mcp_server._sanitize_args`)、一份带验收测试的**硬化路线图(P1–P7)**、以及「无需补丁」的验收标准。每完成一项就在该文 checklist 打勾并提交。

## 原则

- 容错只在「无歧义可还原」时介入;**不得破坏强模型路径或原生契约语义**(`OPENWORKFLOW_LENIENT=0` 必须能恢复严格行为)。
- 每加一项容错都要补 `tests/` fixtures,纳入现有测试套件,防回归。
- 实现完即更新 README 的本地章节,并把 `LOCAL_LLM_HARDENING.md` 里对应的「外部补丁」描述删去/降级。
