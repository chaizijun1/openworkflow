"""The orchestration runtime — builds the sandboxed namespace and runs the script.

This is where the six primitives live. They are bound to a single ``WorkflowRuntime`` so that
``agent()`` calls everywhere (including nested ``workflow()``) share one concurrency cap, one
agent counter, one abort signal, and one token budget — exactly the sharing the original
documents.

Execution model (matching the original):
  * The script's ``async def main()`` runs to completion; its return value is the result.
  * ``agent()`` is the only token-spending primitive. Before each call the hard budget ceiling
    is checked (raise once exhausted). Completed results are journaled for resume.
  * ``parallel`` is a barrier (gather-all, failures -> None). ``pipeline`` runs each item
    through the stages as an independent task (no inter-stage barrier).
  * ``workflow(name)`` runs another workflow inline, one level of nesting only.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .backends import AgentBackend, default_backend
from .budget import Budget
from .journal import Journal
from .progress import Progress
from .registry import WorkflowDef, WorkflowRegistry
from .sandbox import CompiledScript, WorkflowScriptError, compile_script

DEFAULT_CONCURRENCY = 8


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class WorkflowResult:
    result: Any
    agent_count: int
    logs: list[str]
    failures: list[str]
    drift: list[str]
    spent_tokens: int


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class WorkflowRuntime:
    backend: AgentBackend = field(default_factory=default_backend)
    budget: Budget = field(default_factory=Budget)
    registry: WorkflowRegistry = field(default_factory=WorkflowRegistry)
    journal: Journal = field(default_factory=Journal)
    progress: Progress = field(default_factory=Progress)
    concurrency: int = DEFAULT_CONCURRENCY
    cwd: str = field(default_factory=os.getcwd)
    agent_types: dict[str, str] = field(default_factory=dict)  # agentType -> system prompt

    # shared, run-wide state
    _sem: asyncio.Semaphore = field(init=False)
    _agent_count: int = 0
    _logs: list[str] = field(default_factory=list)
    _failures: list[str] = field(default_factory=list)
    _current_phase: str | None = None
    _abort: asyncio.Event = field(default_factory=asyncio.Event)
    _nesting: int = 0  # workflow() depth; >0 means we are inside a child

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.concurrency)

    # ------------------------------------------------------------------ primitives
    async def _agent(self, prompt: str, opts: dict[str, Any] | None = None) -> Any:
        opts = dict(opts or {})
        label = opts.get("label") or _label_of(prompt)
        phase = opts.get("phase", self._current_phase)

        # journal replay: a previously-completed identical call returns cached value for free
        hit, value = self.journal.try_replay(phase, label, prompt)
        if hit:
            self.progress.agent_done(phase, label, replayed=True)
            self._agent_count += 1
            return value

        # hard budget ceiling — once spent reaches total, further agent() calls throw
        if self.budget.exhausted():
            raise BudgetExhausted(
                f"token budget exhausted ({self.budget.spent()}/{self.budget.total})"
            )

        if self._abort.is_set():
            raise asyncio.CancelledError("workflow aborted")

        # resolve a custom agentType to a system-prompt override (honored by tool/text backends)
        call_opts: dict[str, Any] = {**opts, "phase": phase, "label": label}
        atype = opts.get("agentType")
        if atype and atype in self.agent_types:
            call_opts["system"] = self.agent_types[atype]

        # worktree isolation: run this agent in a fresh detached worktree so parallel
        # file-mutating agents don't collide; keep it only if it ended up modified.
        wt = None
        if opts.get("isolation") == "worktree":
            from .worktree import create_worktree
            try:
                wt = await create_worktree(self.cwd)
                if wt:
                    call_opts["cwd"] = wt.path
                else:
                    self._log(f"isolation:worktree ignored — {self.cwd} is not a git repo")
            except Exception as e:  # noqa: BLE001
                self._log(f"worktree setup failed: {e}; running without isolation")

        self.progress.agent_start(phase, label)
        try:
            async with self._sem:
                if self.budget.exhausted():  # re-check after waiting for a slot
                    raise BudgetExhausted(
                        f"token budget exhausted ({self.budget.spent()}/{self.budget.total})"
                    )
                res = await self.backend.run(prompt, call_opts)
        finally:
            if wt:
                from .worktree import cleanup_worktree
                kept = await cleanup_worktree(wt)
                if kept:
                    self._log(f"worktree kept (modified): {kept}")
        self.budget.add(res.output_tokens)
        self._agent_count += 1
        self.journal.record(phase, label, prompt, res.value)
        self.progress.agent_done(phase, label)
        return res.value

    async def _parallel(self, thunks: list[Callable[[], Awaitable[Any]]]) -> list[Any]:
        # BARRIER: await all; a thunk that throws resolves to None (call never rejects).
        async def guard(thunk: Callable[[], Awaitable[Any]]) -> Any:
            try:
                return await _maybe_await(thunk())
            except BudgetExhausted:
                raise  # budget ceiling is fatal, must propagate
            except Exception as e:  # noqa: BLE001 - mirror JS: failures -> null
                self._failures.append(repr(e))
                return None

        return await asyncio.gather(*(guard(t) for t in thunks))

    async def _pipeline(self, items: list[Any], *stages: Callable[..., Any]) -> list[Any]:
        # NO barrier between stages: each item flows through all stages as its own task.
        async def chain(item: Any, index: int) -> Any:
            prev: Any = item
            for stage in stages:
                try:
                    prev = await _maybe_await(stage(prev, item, index))
                except BudgetExhausted:
                    raise
                except Exception as e:  # noqa: BLE001 - drop item to None, skip rest
                    self._failures.append(repr(e))
                    return None
            return prev

        return await asyncio.gather(*(chain(it, i) for i, it in enumerate(items)))

    def _phase(self, title: str) -> None:
        self._current_phase = title
        self.progress.phase(title)

    def _log(self, message: str) -> None:
        message = str(message)
        self._logs.append(message)
        self.progress.log(message)

    async def _workflow(self, name_or_ref: Any, args: Any = None) -> Any:
        # one level of nesting only
        if self._nesting >= 1:
            raise WorkflowScriptError("workflow() cannot be nested more than one level deep")
        if isinstance(name_or_ref, dict) and "scriptPath" in name_or_ref:
            wdef = self.registry.from_script_path(name_or_ref["scriptPath"])
        elif isinstance(name_or_ref, str):
            wdef = self.registry.get(name_or_ref)
            if wdef is None:
                avail = ", ".join(sorted(self.registry.all())) or "(none)"
                raise WorkflowScriptError(
                    f"no workflow named '{name_or_ref}'. Available: {avail}"
                )
        else:
            raise WorkflowScriptError("workflow() takes a name string or {scriptPath: ...}")

        compiled = compile_script(wdef.script, filename=wdef.file_path)
        self._nesting += 1
        try:
            # child shares this runtime's budget/sem/journal/counter; only `args` differs
            return await self._exec_compiled(compiled, args)
        finally:
            self._nesting -= 1

    # ------------------------------------------------------------------ execution
    def _build_namespace(self, args: Any) -> dict[str, Any]:
        ns: dict[str, Any] = {
            "agent": self._agent,
            "parallel": self._parallel,
            "pipeline": self._pipeline,
            "phase": self._phase,
            "log": self._log,
            "workflow": self._workflow,
            "args": args,
            "budget": self.budget,
            "console": _Console(self._log),
            "print": lambda *a, **k: self._log(" ".join(str(x) for x in a)),
        }
        return ns

    async def _exec_compiled(self, compiled: CompiledScript, args: Any) -> Any:
        ns = self._build_namespace(args)
        exec(compiled.code, ns)  # noqa: S102 - intentional: this is the script runner
        main = ns.get("main")
        if main is None or not inspect.iscoroutinefunction(main):
            raise WorkflowScriptError("script must define `async def main():` as its entrypoint")
        return await main()

    async def run(self, wdef: WorkflowDef, args: Any = None) -> WorkflowResult:
        compiled = compile_script(wdef.script, filename=wdef.file_path)
        result = await self._exec_compiled(compiled, args)
        return WorkflowResult(
            result=result,
            agent_count=self._agent_count,
            logs=list(self._logs),
            failures=list(self._failures),
            drift=self.journal.drift_report(),
            spent_tokens=self.budget.spent(),
        )

    def abort(self) -> None:
        self._abort.set()


class _Console:
    """Redirect ``console.log`` into the workflow narrator (matches the JS ``console`` global)."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def log(self, *args: Any) -> None:
        self._sink(" ".join(str(a) for a in args))

    error = log
    warn = log
    info = log


def _label_of(prompt: str, limit: int = 50) -> str:
    line = prompt.strip().splitlines()[0] if prompt.strip() else "agent"
    return line if len(line) <= limit else line[: limit - 1] + "…"


# --------------------------------------------------------------------- convenience

async def run_workflow(
    source_or_def: str | WorkflowDef,
    *,
    args: Any = None,
    backend: AgentBackend | None = None,
    budget_total: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    journal_path: str | None = None,
    resume: bool = False,
    quiet: bool = False,
    cwd: str | None = None,
    secure: bool = False,
) -> WorkflowResult:
    """Compile + run a workflow from raw source, a file path, or a WorkflowDef.

    With ``secure=True`` the script runs inside an OS sandbox (sandbox-exec/bwrap) and the six
    primitives are served over RPC from the trusted host — no network or out-of-scratch writes
    are possible from the script. Otherwise it runs in-process (fast, not a security boundary).
    """
    if secure:
        from .secure import run_workflow_secure
        return await run_workflow_secure(
            source_or_def, args=args, backend=backend, budget_total=budget_total,
            concurrency=concurrency, journal_path=journal_path, resume=resume,
            quiet=quiet, cwd=cwd,
        )
    registry = WorkflowRegistry(cwd=cwd)
    if isinstance(source_or_def, WorkflowDef):
        wdef = source_or_def
    else:
        # treat as inline source
        compiled = compile_script(source_or_def, filename="<inline>")
        wdef = WorkflowDef(
            name=compiled.meta["name"],
            description=compiled.meta.get("description", ""),
            source="scriptPath",
            file_path="<inline>",
            script=source_or_def,
            meta=compiled.meta,
        )
    rt = WorkflowRuntime(
        backend=backend or default_backend(),
        budget=Budget(total=budget_total),
        registry=registry,
        journal=Journal.load(journal_path, resume=resume),
        progress=Progress(quiet=quiet),
        concurrency=concurrency,
        cwd=cwd or os.getcwd(),
        agent_types=load_agent_types(),
    )
    return await rt.run(wdef, args=args)


def load_agent_types() -> dict[str, str]:
    """Load custom agentType system prompts from ~/.openworkflow/agents/*.md (stem -> prompt)."""
    import os as _os
    from pathlib import Path

    d = Path(_os.path.expanduser("~")) / ".openworkflow" / "agents"
    out: dict[str, str] = {}
    if d.is_dir():
        for f in d.glob("*.md"):
            try:
                out[f.stem] = f.read_text(encoding="utf-8")
            except OSError:
                continue
    return out


async def do_task(
    task: str,
    *,
    args: Any = None,
    backend: AgentBackend | None = None,
    budget_total: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    quiet: bool = False,
    cwd: str | None = None,
    max_repairs: int = 2,
    secure: bool = False,
):
    """Autonomously DESIGN a workflow for ``task``, then run it.

    This is the end-to-end "self-designing" path: the brain (author) writes the script, the
    runtime (body) executes it. Returns ``(WorkflowResult, DesignResult)``.
    """
    from .author import design_workflow  # local import to avoid a cycle

    backend = backend or default_backend()
    registry = WorkflowRegistry(cwd=cwd)
    design = await design_workflow(
        task, backend=backend, registry=registry, max_repairs=max_repairs
    )
    if not quiet:
        import sys
        print(
            f"▸ designed workflow '{design.compiled.meta['name']}' "
            f"(by {design.authored_by}, {design.attempts} repair attempt(s))",
            file=sys.stderr,
        )
    result = await run_workflow(
        design.script,
        args=args if args is not None else task,
        backend=backend,
        budget_total=budget_total,
        concurrency=concurrency,
        quiet=quiet,
        cwd=cwd,
        secure=secure,
    )
    return result, design
