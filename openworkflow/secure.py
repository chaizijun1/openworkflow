"""Host-side secure runner — drives a sandboxed workflow over RPC.

The untrusted script runs in ``_sandbox_child`` under an OS sandbox (no network, no writes
outside scratch). This module is the *trusted* side: it spawns that child and answers its RPC
calls by delegating to an ordinary :class:`WorkflowRuntime`. So every heavy/sensitive thing —
the real LLM/tool calls, the API key, the shared budget, the journal, the worktree lifecycle —
stays on the host, exactly where it should be. The sandbox only does orchestration.

This is the faithful analogue of the original's model: the script's container pauses on a tool
call, the call executes (here: on the trusted host), and the result returns to the script.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from typing import Any

from ._rpc import Channel
from .backends import AgentBackend, default_backend
from .budget import Budget
from .journal import Journal
from .progress import Progress
from .registry import WorkflowDef, WorkflowRegistry
from .runtime import (
    DEFAULT_CONCURRENCY,
    BudgetExhausted,
    WorkflowResult,
    WorkflowRuntime,
    load_agent_types,
)
from .sandbox import compile_script
from .seatbelt import SandboxSpec, sandbox_available, sandbox_command


def _minimal_env(scratch: str, read_fd: int, write_fd: int, project_root: str) -> dict[str, str]:
    """A stripped env for the child — notably WITHOUT any API keys or cloud creds."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", scratch),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": scratch,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": project_root,
        "OWF_IN": str(read_fd),
        "OWF_OUT": str(write_fd),
    }
    return env


class SecureRunner:
    """Spawns the sandboxed child and serves its RPC requests from a host WorkflowRuntime."""

    def __init__(self, runtime: WorkflowRuntime, *, scratch: str) -> None:
        self.rt = runtime
        self.scratch = scratch

    async def run(self, wdef: WorkflowDef, args: Any) -> WorkflowResult:
        compile_script(wdef.script, filename=wdef.file_path)  # validate before sandboxing

        # host<-child and child<-host pipes
        r_host, w_child = os.pipe()
        r_child, w_host = os.pipe()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = SandboxSpec(scratch=self.scratch)
        inner = [sys.executable, "-m", "openworkflow._sandbox_child"]
        cmd = sandbox_command(spec, inner)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            pass_fds=(w_child, r_child),
            env=_minimal_env(self.scratch, r_child, w_child, project_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        os.close(w_child)
        os.close(r_child)

        ch = await Channel.from_fds(r_host, w_host)
        await ch.send({"t": "init", "script": wdef.script, "args": args,
                       "budget_total": self.rt.budget.total})

        result_holder: dict[str, Any] = {}
        in_flight: set[asyncio.Task] = set()

        async def handle_agent(msg: dict) -> None:
            mid = msg["id"]
            try:
                value = await self.rt._agent(msg["prompt"], msg.get("opts") or {})
                await ch.send({"t": "reply", "id": mid, "ok": True, "value": value,
                               "spent": self.rt.budget.spent()})
            except BudgetExhausted as e:
                await ch.send({"t": "reply", "id": mid, "ok": False, "fatal": True,
                               "error": str(e), "spent": self.rt.budget.spent()})
            except Exception as e:  # noqa: BLE001
                await ch.send({"t": "reply", "id": mid, "ok": False, "fatal": False,
                               "error": f"{type(e).__name__}: {e}", "spent": self.rt.budget.spent()})

        async def handle_resolve(msg: dict) -> None:
            mid = msg["id"]
            ref = msg.get("ref")
            try:
                if isinstance(ref, dict) and "scriptPath" in ref:
                    wd = self.rt.registry.from_script_path(ref["scriptPath"])
                elif isinstance(ref, str):
                    wd = self.rt.registry.get(ref)
                    if wd is None:
                        avail = ", ".join(sorted(self.rt.registry.all())) or "(none)"
                        raise RuntimeError(f"no workflow named '{ref}'. Available: {avail}")
                else:
                    raise RuntimeError("workflow() takes a name or {scriptPath: ...}")
                await ch.send({"t": "reply", "id": mid, "ok": True,
                               "source": wd.script, "file_path": wd.file_path})
            except Exception as e:  # noqa: BLE001
                await ch.send({"t": "reply", "id": mid, "ok": False, "error": str(e)})

        try:
            while True:
                msg = await ch.recv()
                if msg is None:
                    break
                t = msg.get("t")
                if t == "call":
                    method = msg.get("method")
                    if method == "agent":
                        task = asyncio.ensure_future(handle_agent(msg))
                    elif method == "resolve":
                        task = asyncio.ensure_future(handle_resolve(msg))
                    else:
                        continue
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                elif t == "note":
                    if msg.get("method") == "phase":
                        self.rt._phase(msg.get("title", ""))
                    elif msg.get("method") == "log":
                        self.rt._log(msg.get("message", ""))
                elif t == "done":
                    result_holder["value"] = msg.get("value")
                    break
                elif t == "error":
                    result_holder["error"] = msg.get("error")
                    result_holder["tb"] = msg.get("tb")
                    break
        finally:
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            ch.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        if "error" in result_holder:
            raise RuntimeError(
                f"sandboxed workflow failed: {result_holder['error']}\n{result_holder.get('tb', '')}"
            )
        if "value" not in result_holder:
            stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
            raise RuntimeError(
                "sandboxed child exited without a result (the sandbox may have blocked something "
                f"Python needed). Child stderr:\n{stderr[:2000]}"
            )

        return WorkflowResult(
            result=result_holder["value"],
            agent_count=self.rt._agent_count,
            logs=list(self.rt._logs),
            failures=list(self.rt._failures),
            drift=self.rt.journal.drift_report(),
            spent_tokens=self.rt.budget.spent(),
        )


async def run_workflow_secure(
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
) -> WorkflowResult:
    """Compile + run a workflow inside an OS sandbox, serving primitives from the trusted host."""
    if not sandbox_available():
        raise RuntimeError(
            "no OS sandbox available (need sandbox-exec on macOS or bwrap on Linux). "
            "Run without secure mode to use the in-process executor."
        )
    registry = WorkflowRegistry(cwd=cwd)
    if isinstance(source_or_def, WorkflowDef):
        wdef = source_or_def
    else:
        compiled = compile_script(source_or_def, filename="<inline>")
        wdef = WorkflowDef(
            name=compiled.meta["name"], description=compiled.meta.get("description", ""),
            source="scriptPath", file_path="<inline>", script=source_or_def, meta=compiled.meta,
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
    scratch = os.path.realpath(tempfile.mkdtemp(prefix="owf-scratch-"))
    try:
        return await SecureRunner(rt, scratch=scratch).run(wdef, args=args)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
