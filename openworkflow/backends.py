"""Agent backends — what ``agent()`` actually calls.

The original spawns a *workflow-subagent* (an LLM with tools). Here we abstract that behind
``AgentBackend`` so the orchestration layer can be exercised with zero cost (``MockBackend``)
or wired to the real Anthropic Messages API (``AnthropicBackend``).

A backend returns either:
  * a plain string (the subagent's final text), when no ``schema`` was requested, or
  * a dict validated against the JSON schema, when ``opts["schema"]`` is given (the original
    forces a ``StructuredOutput`` tool call so no parsing is needed on the script side).

It reports output tokens via the ``AgentResult.output_tokens`` field so the shared budget can
be updated by the runtime.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mcp_client import MCPManager
    from .tools import ToolBox

# System prompt for the default workflow subagent, adapted from the original's
# `workflow-subagent` definition ("Internal subagent for workflow script orchestration").
WORKFLOW_SUBAGENT_SYSTEM = (
    "You are a workflow subagent: a focused worker spawned by an orchestration script. "
    "Complete the single task in the prompt directly and return only the result the script "
    "asked for — no preamble, no acknowledgements. Be concise and concrete."
)


@dataclass
class AgentResult:
    """What a backend hands back to the runtime."""

    value: Any
    output_tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class AgentBackend(ABC):
    """Pluggable LLM backend for ``agent()``."""

    name: str = "base"

    @abstractmethod
    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        """Execute one subagent call.

        ``opts`` may contain: label, phase, schema, model, agentType, isolation.
        ``schema`` (a JSON Schema dict) means: return a dict validated against it.
        """
        raise NotImplementedError


class MockBackend(AgentBackend):
    """Deterministic, zero-cost backend for testing orchestration logic.

    No network, no key. Echoes a compact summary of the prompt, and for schema calls fabricates
    a value per declared property type. Token counts are estimated (chars/4) so budget logic is
    still exercised. Fully deterministic (no clocks/RNG) so it composes with journal replay.
    """

    name = "mock"

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        label = opts.get("label") or _first_line(prompt)
        schema = opts.get("schema")
        if schema:
            value: Any = _fabricate(schema, seed=label)
            text = json.dumps(value, ensure_ascii=False)
        else:
            value = f"[mock:{label}] " + _condense(prompt)
            text = value
        tokens = max(1, len(text) // 4)
        return AgentResult(value=value, output_tokens=tokens, meta={"backend": "mock"})


class AnthropicBackend(AgentBackend):
    """Real backend hitting the Anthropic Messages API (proper API, never ACP/passthrough).

    Uses prompt caching on the (frozen) system prompt. For schema calls it forces a
    ``StructuredOutput`` tool, mirroring the original. Activated automatically by
    ``default_backend()`` when ``ANTHROPIC_API_KEY`` is present.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        default_model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 4096,
        system: str = WORKFLOW_SUBAGENT_SYSTEM,
        client: Any = None,
    ) -> None:
        self.default_model = default_model
        self.max_tokens = max_tokens
        self.system = system
        self._client = client  # lazy

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "AnthropicBackend requires the 'anthropic' package: pip install anthropic"
                ) from e
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        client = self._get_client()
        model = opts.get("model") or self.default_model
        schema = opts.get("schema")
        system_text = opts.get("system") or self.system  # agentType system-prompt override
        system_blocks = [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ]
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=self.max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": prompt}],
        )
        if schema:
            kwargs["tools"] = [
                {
                    "name": "StructuredOutput",
                    "description": "Return the result as a structured object.",
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "StructuredOutput"}

        resp = await client.messages.create(**kwargs)
        out_tokens = getattr(resp.usage, "output_tokens", 0) if getattr(resp, "usage", None) else 0

        if schema:
            value: Any = None
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    value = block.input
                    break
        else:
            value = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            )
        return AgentResult(value=value, output_tokens=out_tokens, meta={"backend": "anthropic", "model": model})


TOOL_AGENT_SYSTEM = (
    "You are a workflow subagent with tools. Complete the task in the prompt by USING your tools "
    "(read/write files, run shell, search) — do real work, don't just describe it. When finished, "
    "give a concise final answer. If a StructuredOutput tool is provided, call it with the result "
    "when you are done instead of writing the answer as text."
)


class ToolAgentBackend(AgentBackend):
    """A subagent that runs a real multi-turn tool-use loop (the faithful subagent).

    Mirrors the original's ``tools:["*"]`` subagents: the model alternates between thinking and
    calling tools (Read/Write/Edit/Bash/Grep/Glob), and we execute each tool locally and feed the
    result back, until it ends its turn (or calls StructuredOutput for a schema result). Output
    tokens across every turn are summed for the shared budget.

    A ``client`` can be injected for testing; otherwise an ``anthropic.AsyncAnthropic`` is used.
    """

    name = "tool-agent"

    def __init__(
        self,
        *,
        default_model: str = "claude-sonnet-4-6",
        max_tokens: int = 8192,
        max_iterations: int = 20,
        system: str = TOOL_AGENT_SYSTEM,
        toolbox: "ToolBox | None" = None,
        client: Any = None,
        mcp: "MCPManager | None" = None,
    ) -> None:
        from .tools import ToolBox

        self.default_model = default_model
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.system = system
        self.toolbox = toolbox or ToolBox()
        self._client = client
        self.mcp = mcp  # optional: connects the MCP tool ecosystem
        self._mcp_ready = False

    async def _ensure_mcp(self) -> None:
        if self.mcp is not None and not self._mcp_ready:
            await self.mcp.connect()
            self._mcp_ready = True

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("ToolAgentBackend requires: pip install anthropic") from e
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def _toolbox_for(self, opts: dict[str, Any]) -> "ToolBox":
        """Per-call toolbox: scope to opts['cwd'] when a worktree (or override dir) is given."""
        cwd = opts.get("cwd")
        t = self.toolbox
        if cwd and cwd != t.cwd:
            from .tools import ToolBox
            return ToolBox(cwd=cwd, confine=t.confine, allow_bash=t.allow_bash,
                           bash_timeout=t.bash_timeout)
        return t

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        client = self._get_client()
        await self._ensure_mcp()
        model = opts.get("model") or self.default_model
        schema = opts.get("schema")
        toolbox = self._toolbox_for(opts)

        tool_specs = list(toolbox.specs())
        if self.mcp is not None:
            tool_specs += self.mcp.tool_specs()
        if schema:
            tool_specs.append(
                {
                    "name": "StructuredOutput",
                    "description": "Call this with the final result when done.",
                    "input_schema": schema,
                }
            )

        system_text = opts.get("system") or self.system  # agentType system-prompt override
        system_blocks = [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        total_out = 0

        for _ in range(self.max_iterations):
            resp = await client.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=system_blocks,
                tools=tool_specs,
                messages=messages,
            )
            if getattr(resp, "usage", None):
                total_out += getattr(resp.usage, "output_tokens", 0)

            content = list(resp.content)
            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]

            if not tool_uses:  # end of turn → text answer
                text = "".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
                return AgentResult(value=text, output_tokens=total_out, meta={"backend": "tool-agent", "model": model})

            # record the assistant turn verbatim, then answer each tool call
            messages.append({"role": "assistant", "content": _blocks_to_dicts(content)})
            results = []
            structured_value = None
            for tu in tool_uses:
                if schema and tu.name == "StructuredOutput":
                    structured_value = tu.input
                    out = "ok"
                elif self.mcp is not None and self.mcp.handles(tu.name):
                    out = await self.mcp.execute(tu.name, tu.input)
                else:
                    out = await toolbox.execute(tu.name, tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            messages.append({"role": "user", "content": results})

            if structured_value is not None:  # schema satisfied → done
                return AgentResult(value=structured_value, output_tokens=total_out,
                                   meta={"backend": "tool-agent", "model": model})

        return AgentResult(
            value="[tool-agent: hit max iterations without finishing]",
            output_tokens=total_out,
            meta={"backend": "tool-agent", "truncated": True},
        )


def _blocks_to_dicts(content: list) -> list[dict]:
    """Normalize SDK content blocks back into request-shaped dicts for the next turn."""
    out = []
    for b in content:
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def default_backend() -> AgentBackend:
    """AnthropicBackend if a key is configured, otherwise the zero-cost MockBackend."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicBackend()
    return MockBackend()


# --------------------------------------------------------------------------- helpers

def _first_line(s: str) -> str:
    return s.strip().splitlines()[0][:60] if s.strip() else "task"


def _condense(s: str, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fabricate(schema: dict[str, Any], seed: str = "") -> Any:
    """Produce a deterministic placeholder value matching a JSON-schema node."""
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: _fabricate(v, seed) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_fabricate(schema.get("items", {"type": "string"}), seed)]
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if "enum" in schema:
        return schema["enum"][0]
    return f"[mock:{seed}]" if seed else "mock"
