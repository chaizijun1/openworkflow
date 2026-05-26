"""The *author* layer — autonomously design a workflow from a natural-language task.

This is the half that makes the original feel autonomous: in Claude Code the main model
*writes the orchestration script itself*, then hands it to the `Workflow` tool. Our runtime is
the body (it executes scripts); this module is the brain (it authors them).

Given a task, ``design_workflow`` prompts an LLM backend to emit a Python workflow script that
obeys the same contract the runtime enforces (``meta`` first, ``async def main()``, the six
primitives, no clocks/RNG). The generated script is validated with the very same
``compile_script`` the runtime uses; on a validation error the error text is fed back for a
repair attempt — mirroring how the original iterates on a script via ``{scriptPath}``.

Offline (MockBackend / no API key) there is no real LLM to write code, so a deterministic
*scaffold designer* produces a valid, structurally-sensible script instead — enough to exercise
the full design→validate→run loop without spending tokens. With ``ANTHROPIC_API_KEY`` set, the
design is genuinely authored by the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .backends import AgentBackend, MockBackend
from .registry import WorkflowRegistry
from .sandbox import CompiledScript, WorkflowScriptError, compile_script

AUTHORING_SYSTEM = """You design *orchestration scripts* for a multi-agent workflow runtime.

Output ONLY a single Python code block — no prose before or after. The script MUST obey:

1. The FIRST statement is a literal `meta = {"name": ..., "description": ..., "phases": [...]}`.
2. It defines `async def main():` as the entrypoint; its return value is the result.
3. Inside main, these globals are in scope (do NOT import or define them):
   - await agent(prompt, opts?) -> spawn a subagent. opts can include
       {"label": str, "phase": str, "schema": <JSON schema dict>}. With a schema you get back a
       validated dict; without one you get the subagent's text.
   - await parallel([thunk, ...]) -> BARRIER: runs zero-arg callables concurrently, awaits all.
       A failing thunk becomes None, so filter results with `[x for x in r if x]`.
       Build thunks with default-arg lambdas to capture loop vars: lambda a=a: agent(...).
   - await pipeline(items, stage1, stage2, ...) -> each item flows through all stages with NO
       barrier between stages. Each stage is `async def s(prev, item, index): ...`.
   - phase(title) -> start a progress group; log(message) -> narrator line.
   - await workflow(name, args) -> run another saved workflow inline (one level only).
   - args -> the task input. budget -> {total, spent(), remaining()} hard token ceiling.
4. NO clocks or randomness: do not use time, datetime, random, secrets, or uuid (breaks resume).

Pick the structure that fits the task: parallel fan-out + synthesis for research/breadth;
pipeline for per-item multi-stage work; a single agent for trivial tasks. Keep it concise."""


@dataclass
class DesignResult:
    script: str
    compiled: CompiledScript
    attempts: int
    authored_by: str  # "llm" | "scaffold"


def _extract_code(text: str) -> str:
    """Pull the Python out of a ```python ...``` fence, or return the text as-is."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _build_prompt(task: str, registry: WorkflowRegistry) -> str:
    saved = registry.all()
    catalog = ""
    if saved:
        catalog = "\nSaved workflows you may call via workflow(name, args):\n" + "\n".join(
            f"  - {n}: {d.description}" for n, d in sorted(saved.items())
        )
    return f"Design a workflow for this task:\n\n{task}\n{catalog}"


def _repair_prompt(task: str, code: str, error: str) -> str:
    return (
        f"Design a workflow for this task:\n\n{task}\n\n"
        f"Your previous script was rejected by the validator:\n{error}\n\n"
        f"Here is what you wrote — fix it and output the corrected full script:\n"
        f"```python\n{code}\n```"
    )


def _scaffold(task: str, registry: WorkflowRegistry) -> str:
    """Deterministic offline designer: choose a structure from simple task cues.

    Real autonomy comes from the LLM path; this keeps design→validate→run runnable with no key.
    """
    t = task.lower()
    per_item = any(k in t for k in ("each", "every", "per ", "files", "list of", "for all"))
    safe_task = task.replace('"', "'")

    if per_item:
        return f'''meta = {{
    "name": "auto-pipeline",
    "description": "Auto-designed pipeline for: {safe_task[:80]}",
    "phases": ["analyze", "act"],
}}

async def main():
    items = args if isinstance(args, list) else [args]

    async def analyze(prev, item, index):
        phase("analyze")
        return await agent(f"Analyze item #{{index}} for the task: {safe_task}\\nItem: {{item}}",
                           {{"label": f"analyze:{{item}}", "phase": "analyze"}})

    async def act(prev, item, index):
        phase("act")
        return await agent(f"Given this analysis, produce the result for {{item}}:\\n{{prev}}",
                           {{"label": f"act:{{item}}", "phase": "act"}})

    log(f"pipelining {{len(items)}} item(s)")
    results = await pipeline(items, analyze, act)
    return {{str(it): r for it, r in zip(items, results)}}
'''

    fleet = "(budget.total // 100000) if budget.total else 4"
    return f'''meta = {{
    "name": "auto-fanout",
    "description": "Auto-designed parallel fan-out + synthesis for: {safe_task[:80]}",
    "phases": ["decompose", "explore", "synthesize"],
}}

async def main():
    task = args or "{safe_task}"

    phase("decompose")
    plan = await agent(f"Break this task into independent subtasks:\\n{{task}}",
                       {{"label": "decompose",
                         "schema": {{"type": "object",
                                    "properties": {{"subtasks": {{"type": "array",
                                                                 "items": {{"type": "string"}}}}}}}}}})
    subtasks = (plan or {{}}).get("subtasks") or [task]
    log(f"{{len(subtasks)}} subtask(s)")

    phase("explore")
    parts = await parallel([(lambda s=s: agent(f"Handle subtask: {{s}}", {{"phase": "explore"}}))
                            for s in subtasks])
    parts = [p for p in parts if p]

    phase("synthesize")
    return await agent("Synthesize these into a final answer:\\n" +
                       "\\n".join(f"- {{p}}" for p in parts),
                       {{"label": "synthesize"}})
'''


async def design_workflow(
    task: str,
    *,
    backend: AgentBackend,
    registry: WorkflowRegistry | None = None,
    max_repairs: int = 2,
) -> DesignResult:
    """Autonomously author a validated workflow script for ``task``."""
    registry = registry or WorkflowRegistry()

    # Offline / no real authoring model -> deterministic scaffold designer.
    if isinstance(backend, MockBackend):
        code = _scaffold(task, registry)
        return DesignResult(script=code, compiled=compile_script(code), attempts=0,
                            authored_by="scaffold")

    prompt = _build_prompt(task, registry)
    last_err = ""
    last_code = ""
    for attempt in range(max_repairs + 1):
        res = await backend.run(prompt, {"label": "design-workflow"})
        text = res.value if isinstance(res.value, str) else str(res.value)
        code = _extract_code(text)
        last_code = code
        try:
            compiled = compile_script(code)
            return DesignResult(script=code, compiled=compiled, attempts=attempt,
                                authored_by="llm")
        except WorkflowScriptError as e:
            last_err = str(e)
            prompt = _repair_prompt(task, code, last_err)

    raise WorkflowScriptError(
        f"failed to author a valid workflow after {max_repairs + 1} attempt(s); "
        f"last error: {last_err}\n--- last script ---\n{last_code}"
    )
