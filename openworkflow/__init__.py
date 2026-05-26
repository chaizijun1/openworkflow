"""openworkflow — a faithful Python re-implementation of Claude Code's hidden Workflow tool.

The original (shipped in Claude Code 2.1.150 as an undocumented `Workflow` tool) lets the
main model write a *sandboxed orchestration script* that drives many subagents. Tool calls
made inside the script pause the container, execute, and return their result to the running
*script* — not to the model's context window — so only the script's final return value flows
back to the model. That is the whole point: cheap fan-out over many agents/tool calls without
flooding the context.

This package mirrors that design in Python:

  * Scripts are Python modules whose first statement is a ``meta = {...}`` literal and which
    define an ``async def main():`` entrypoint.
  * Inside ``main`` six globals are in scope, matching the originals:
        agent(prompt, opts)        - spawn a subagent (LLM call), optional JSON-schema output
        parallel(thunks)           - BARRIER fan-out; failures map to None
        pipeline(items, *stages)   - no-barrier per-item staging
        phase(title)               - progress grouping
        log(message)               - narrator line
        workflow(name_or_ref, args)- run another workflow inline (one level of nesting)
    plus the data globals ``args`` and ``budget``, and a redirected ``console``/``print``.
  * A shared token ``budget`` acts as a HARD ceiling across the run and all nested workflows.
  * ``Date/time`` helpers are banned in scripts (nondeterminism breaks resume), exactly as in
    the original.
  * Completed ``agent()`` results are journaled so a crashed run can ``--resume`` and replay
    finished work instead of re-spending tokens, with drift detection.
"""

from .author import DesignResult, design_workflow
from .backends import AgentBackend, AnthropicBackend, MockBackend, ToolAgentBackend, default_backend
from .budget import Budget
from .mcp_client import MCPManager, load_mcp_config
from .registry import WorkflowDef, WorkflowRegistry
from .runtime import WorkflowResult, WorkflowRuntime, do_task, run_workflow
from .secure import run_workflow_secure
from .seatbelt import SandboxSpec, sandbox_available
from .tools import ToolBox

__all__ = [
    "AgentBackend",
    "AnthropicBackend",
    "MockBackend",
    "ToolAgentBackend",
    "ToolBox",
    "MCPManager",
    "load_mcp_config",
    "default_backend",
    "Budget",
    "DesignResult",
    "design_workflow",
    "do_task",
    "WorkflowDef",
    "WorkflowRegistry",
    "WorkflowResult",
    "WorkflowRuntime",
    "run_workflow",
    "run_workflow_secure",
    "SandboxSpec",
    "sandbox_available",
]
