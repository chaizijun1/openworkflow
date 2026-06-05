"""Expose openworkflow to Claude Code (and any MCP client) as a ``Workflow`` tool.

The native `Workflow` tool is hidden/unavailable in most Claude Code versions. This module wraps
openworkflow as an MCP server so the model can call ``Workflow(...)`` exactly like the original —
it writes an orchestration script (or names a saved one), the server runs it on the host, and
only the final result returns to the model's context.

Register it with Claude Code (any version with MCP support):

    claude mcp add openworkflow -- python3 -m openworkflow.mcp_server

or in ``.mcp.json``:

    {"mcpServers": {"openworkflow": {"command": "python3", "args": ["-m", "openworkflow.mcp_server"]}}}

Then the model has a `Workflow` tool. Where does the subagent LLM come from?

  * If ``ANTHROPIC_API_KEY`` is set → subagents run a real tool-using loop (``ToolAgentBackend``),
    so they can read/write files, run shell, fetch web, use MCP tools. Most capable, billed
    per-token via the API — the recommended path for any real volume.
  * Otherwise (no key) → ``auto`` falls back to ``MockBackend`` (safe no-op). It does **NOT**
    silently use the client's subscription.

⚠️ **Sampling and subscription risk.** You can opt in to ``OPENWORKFLOW_BACKEND=sampling`` (text)
or ``sampling-tools`` (text + local tools) to route subagent reasoning to the *client's own
model* — i.e. the user's Claude Code subscription. This is the most faithful to the original, but
a fan-out workflow issues many completions; on a consumer subscription that can hit rate limits
and may run afoul of Terms of Service (programmatically scaling consumer access). It is therefore
**opt-in only**, and when enabled we clamp concurrency to 1 and apply a conservative default
budget, and warn on stderr. For real workloads, use an API key.

Override with ``OPENWORKFLOW_BACKEND=tool|anthropic|sampling|sampling-tools|mock`` and
``OPENWORKFLOW_SECURE=1`` to run the script in the OS sandbox.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .backends import AgentBackend, AgentResult, AnthropicBackend, MockBackend, ToolAgentBackend
from .mcp_client import MCPManager
from .registry import WorkflowRegistry
from .runtime import run_workflow
from .sandbox import _is_workflow_source, normalize_script

# FastMCP evaluates tool annotations against module globals (we use PEP 563 string annotations),
# so `Context` must be importable here. It's optional — fall back to Any when the SDK is absent.
try:
    from mcp.server.fastmcp import Context
except Exception:  # noqa: BLE001 - mcp is an optional dependency
    Context = Any  # type: ignore[assignment,misc]


class SamplingBackend(AgentBackend):
    """Subagent backend that calls back into the MCP *client's* model via sampling.

    Lets ``agent()`` use Claude Code's own session model — the most faithful analogue of the
    original's subagents — at the cost of being text-only (sampling has no tool loop).
    """

    name = "sampling"

    def __init__(self, ctx: Any, *, max_tokens: int = 4096) -> None:
        self._ctx = ctx
        self.max_tokens = max_tokens

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        # FastMCP Context exposes the client-sampling primitive; signatures vary by SDK version.
        session = getattr(self._ctx, "session", None)
        text = ""
        if session is not None and hasattr(session, "create_message"):
            from mcp.types import SamplingMessage, TextContent

            resp = await session.create_message(
                messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
                max_tokens=self.max_tokens,
            )
            content = getattr(resp, "content", None)
            text = getattr(content, "text", "") if content is not None else ""
        elif hasattr(self._ctx, "sample"):
            resp = await self._ctx.sample(prompt, max_tokens=self.max_tokens)
            text = getattr(resp, "text", str(resp))
        else:
            raise RuntimeError("MCP client does not support sampling; set OPENWORKFLOW_BACKEND or ANTHROPIC_API_KEY")
        return AgentResult(value=text, output_tokens=max(1, len(text) // 4),
                           meta={"backend": "sampling"})


class SamplingToolBackend(AgentBackend):
    """Tool-using subagent whose *reasoning* runs on the client's model via MCP sampling.

    MCP sampling is a plain text completion (no tools field), so we layer a ReAct-style text
    protocol on top: each turn the client model emits a JSON object — either a tool call or a
    final answer — we execute tool calls locally (ToolBox + optional MCP) and feed the
    observation back, then sample again. Net effect: the subagent uses the client's model AND
    gets real tools. Less rigid than native tool_use (the model must follow the JSON protocol),
    but it gives method-A subagents the ability to actually do work.
    """

    name = "sampling-tools"

    def __init__(self, ctx: Any, *, toolbox: Any = None, mcp: Any = None,
                 max_iterations: int = 16, max_tokens: int = 4096) -> None:
        from .tools import ToolBox
        self._ctx = ctx
        self.toolbox = toolbox or ToolBox()
        self.mcp = mcp
        self.max_iterations = max_iterations
        self._sampler = SamplingBackend(ctx, max_tokens=max_tokens)
        self._mcp_ready = False

    async def _sample(self, messages: list[dict]) -> str:
        # reuse SamplingBackend's client-call by flattening the transcript into one prompt
        transcript = "\n\n".join(f"[{m['role']}]\n{m['text']}" for m in messages)
        res = await self._sampler.run(transcript, {})
        return res.value if isinstance(res.value, str) else str(res.value)

    def _tool_specs(self) -> list[dict]:
        specs = list(self.toolbox.specs())
        if self.mcp is not None:
            specs += self.mcp.tool_specs()
        return specs

    async def _exec_tool(self, name: str, tool_input: dict) -> str:
        if self.mcp is not None and self.mcp.handles(name):
            return await self.mcp.execute(name, tool_input)
        return await self.toolbox.execute(name, tool_input)

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        if self.mcp is not None and not self._mcp_ready:
            await self.mcp.connect()
            self._mcp_ready = True
        schema = opts.get("schema")
        tools_doc = "\n".join(f"- {s['name']}: {s.get('description', '')}" for s in self._tool_specs())
        final_shape = ("a JSON object matching the requested schema" if schema else "a string")
        system = (
            "You are a subagent that completes the task by USING TOOLS. Work step by step. "
            "On EVERY turn respond with ONLY a single JSON object and nothing else:\n"
            '  to use a tool:   {"tool": "<name>", "input": { ... }}\n'
            f'  when finished:   {{"final": <{final_shape}>}}\n'
            "Do not wrap the JSON in prose. Available tools:\n" + tools_doc
        )
        messages = [{"role": "user", "text": f"{system}\n\nTASK:\n{prompt}"}]
        total = 0
        last_text = ""
        for _ in range(self.max_iterations):
            text = await self._sample(messages)
            last_text = text
            total += max(1, len(text) // 4)
            obj = _extract_json(text)
            if obj is None:
                return AgentResult(value=text, output_tokens=total, meta={"backend": self.name})
            if "final" in obj:
                return AgentResult(value=obj["final"], output_tokens=total, meta={"backend": self.name})
            if "tool" in obj:
                out = await self._exec_tool(obj["tool"], obj.get("input") or {})
                messages.append({"role": "assistant", "text": text})
                messages.append({"role": "user", "text": f"Observation from {obj['tool']}:\n{out}"})
                continue
            return AgentResult(value=text, output_tokens=total, meta={"backend": self.name})
        return AgentResult(value=last_text or "[sampling-tools: hit max iterations]",
                           output_tokens=total, meta={"backend": self.name, "truncated": True})


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model turn (tolerant of fences / surrounding prose)."""
    import re

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    break
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def make_backend(ctx: Any = None) -> AgentBackend:
    """Pick the subagent backend from env / available capabilities."""
    mode = os.environ.get("OPENWORKFLOW_BACKEND", "auto").lower()
    if mode == "mock":
        return MockBackend()
    if mode == "anthropic":
        return AnthropicBackend()
    if mode == "tool":
        return ToolAgentBackend(mcp=MCPManager())
    if mode == "sampling":            # client model, text-only
        return SamplingBackend(ctx)
    if mode == "sampling-tools":      # client model + tools via ReAct loop (opt-in)
        return SamplingToolBackend(ctx, max_iterations=SAMPLING_MAX_ITERATIONS)
    # auto: real tool_use if we have a key. We DELIBERATELY do NOT fall back to sampling here —
    # sampling silently spends the Claude Code user's subscription quota (rate-limit / ToS risk).
    # To use the client's model you must opt in explicitly with OPENWORKFLOW_BACKEND=sampling[-tools].
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ToolAgentBackend(mcp=MCPManager())
    return MockBackend()


# --- sampling guardrails -----------------------------------------------------
# Sampling routes subagent calls to the MCP client's model — i.e. the user's Claude Code
# subscription. To avoid burst usage that could hit rate limits or anti-abuse heuristics, we
# clamp concurrency and apply a conservative default budget, and we warn (once) on stderr.
SAMPLING_DEFAULT_BUDGET = 40_000     # output-token ceiling when none is given
SAMPLING_MAX_ITERATIONS = 8          # cap ReAct turns per subagent
_warned_sampling = False


def _is_sampling(backend: AgentBackend) -> bool:
    return getattr(backend, "name", "") in ("sampling", "sampling-tools")


def _sampling_guardrails(backend: AgentBackend, budget: int | None) -> tuple[int | None, int, str | None]:
    """Return (budget, concurrency, warning) with sampling clamps applied."""
    from .runtime import DEFAULT_CONCURRENCY
    if _is_sampling(backend):
        eff_budget = budget if budget is not None else SAMPLING_DEFAULT_BUDGET
        warning = (
            "openworkflow: subagents are using MCP SAMPLING — this draws on the Claude Code "
            "client's model/subscription quota. Concurrency is forced to 1 and a default budget "
            f"of {SAMPLING_DEFAULT_BUDGET} output tokens is applied to limit burst usage. "
            "High-volume automated use of a consumer subscription may hit rate limits or violate "
            "Terms of Service — for real workloads set ANTHROPIC_API_KEY (OPENWORKFLOW_BACKEND=tool)."
        )
        return eff_budget, 1, warning
    return budget, DEFAULT_CONCURRENCY, None


def _warn_once(message: str) -> None:
    global _warned_sampling
    if not _warned_sampling:
        print(f"[openworkflow] {message}", file=sys.stderr, flush=True)
        _warned_sampling = True


# --- tolerance for weaker models (e.g. local 27B) that mangle the tool args ------------------
# Gated by OPENWORKFLOW_LENIENT (default ON; set 0/false to restore the strict native contract).
# The heavy lifting (unwrapping mangled source) lives in sandbox.normalize_script; here we add the
# argument-level coercions: nullish defaults and rerouting source mis-placed into name/scriptPath.
def _lenient() -> bool:
    return os.environ.get("OPENWORKFLOW_LENIENT", "1").strip().lower() not in ("0", "false", "no", "off")


def _nullish(v: Any) -> Any:
    """Weaker models often pass an absent optional arg as the string ``"null"``/``"none"``/``""``."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("null", "none", "undefined", "") else s


def _sanitize_args(script: Any, name: Any, scriptPath: Any) -> tuple[Any, Any, Any]:
    """Coerce weak-model tool args back onto the native contract (no-op when not lenient)."""
    if not _lenient():
        return script, name, scriptPath
    script, name, scriptPath = _nullish(script), _nullish(name), _nullish(scriptPath)
    if isinstance(script, str) and script:
        script = normalize_script(script)
    else:
        # a weak model sometimes dumps the whole source into `name` or `scriptPath` instead of
        # `script` — reroute it, but only when it genuinely normalizes to a workflow (so a plain
        # saved-workflow name / real file path is left alone).
        if isinstance(name, str) and name and _is_workflow_source(normalize_script(name)):
            script, name = normalize_script(name), None
        elif isinstance(scriptPath, str) and scriptPath and _is_workflow_source(normalize_script(scriptPath)):
            script, scriptPath = normalize_script(scriptPath), None
    return script, name, scriptPath


async def execute_workflow(
    *,
    script: str | None = None,
    name: str | None = None,
    scriptPath: str | None = None,
    args: Any = None,
    budget: int | None = None,
    backend: AgentBackend | None = None,
    ctx: Any = None,
) -> str:
    """Core handler (FastMCP-independent, so it's directly testable).

    Mirrors the original Workflow tool's inputs: an inline ``script``, a saved ``name``, or a
    ``scriptPath``. Returns the workflow's final result serialized for the model's context.
    """
    backend = backend or make_backend(ctx)
    secure = os.environ.get("OPENWORKFLOW_SECURE") == "1"
    budget, concurrency, warning = _sampling_guardrails(backend, budget)
    if warning:
        _warn_once(warning)

    script, name, scriptPath = _sanitize_args(script, name, scriptPath)   # 容错:弱模型把 script 套引号 / 传 "null"
    if script is not None:
        source: Any = script
    elif name is not None:
        wdef = WorkflowRegistry().get(name)
        if wdef is None:
            avail = ", ".join(sorted(WorkflowRegistry().all())) or "(none)"
            return f"error: no workflow named '{name}'. Available: {avail}"
        source = wdef
    elif scriptPath is not None:
        source = WorkflowRegistry().from_script_path(scriptPath)
    else:
        return ("error: provide one of `script` (inline source), `name` (saved workflow), "
                "or `scriptPath`.")

    try:
        result = await run_workflow(
            source, args=args, backend=backend, budget_total=budget,
            concurrency=concurrency, quiet=True, secure=secure
        )
    except Exception as e:  # noqa: BLE001 - report cleanly to the model
        return f"error: {type(e).__name__}: {e}"

    payload = result.result
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    footer = f"\n\n— {result.agent_count} agent(s), {result.spent_tokens} output tokens"
    if result.failures:
        footer += f", {len(result.failures)} failure(s)"
    return body + footer


def list_workflows() -> str:
    defs = WorkflowRegistry().all()
    if not defs:
        return "(no saved workflows)"
    return "\n".join(f"- {n} [{d.source}]: {d.description}" for n, d in sorted(defs.items()))


def build_server():
    """Construct the FastMCP server (imported lazily so the package has no hard mcp dep)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("openworkflow")

    @mcp.tool(
        description=(
            "Run a multi-agent orchestration workflow. Write a self-contained Python script in "
            "`script` whose first statement is `meta = {'name': ...}` and which defines "
            "`async def main():` using the in-scope primitives agent(prompt, opts), "
            "parallel(thunks), pipeline(items, *stages), phase(title), log(msg), "
            "workflow(name, args), plus `args` and `budget`. Or pass `name` to run a saved "
            "workflow, or `scriptPath`. Only the final return value comes back to you — fan out "
            "over many agents without flooding context. ONLY use when the user explicitly opted "
            "into multi-agent orchestration."
        )
    )
    async def Workflow(  # noqa: N802 - match the original tool name
        ctx: Context,
        script: str | None = None,
        name: str | None = None,
        scriptPath: str | None = None,
        args: Any = None,
        budget: int | None = None,
    ) -> str:
        return await execute_workflow(script=script, name=name, scriptPath=scriptPath,
                                      args=args, budget=budget, ctx=ctx)

    @mcp.tool(description="List saved openworkflow workflows (user/project/built-in).")
    async def WorkflowList() -> str:  # noqa: N802
        return list_workflows()

    return mcp


def main() -> None:
    build_server().run()  # stdio transport by default


if __name__ == "__main__":
    main()
