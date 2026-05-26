"""Sandboxed child entry point — runs the untrusted workflow script inside the OS sandbox.

Launched as ``python3 -m openworkflow._sandbox_child`` under sandbox-exec/bwrap with two inherited
pipe fds (numbers in ``OWF_IN`` / ``OWF_OUT``). It:

  1. reads an ``init`` message (script source, args, budget total) from the host,
  2. builds the six primitives — ``agent``/``workflow`` cross the RPC boundary to the trusted
     host; ``parallel``/``pipeline``/``phase``/``log`` run locally (pure orchestration); ``budget``
     is a local mirror kept in sync from each agent reply,
  3. ``exec``s the script and ``await``s ``main()``,
  4. sends the (JSON-able) result back, or an error with traceback.

The script never touches the network or the filesystem (the kernel forbids it); its only outward
channel is this pipe, which exposes nothing but workflow orchestration calls.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import sys
import traceback
from typing import Any, Awaitable, Callable

from ._rpc import Channel
from .sandbox import compile_script


class _BudgetExhausted(RuntimeError):
    pass


class _ChildBudget:
    def __init__(self, total: int | None) -> None:
        self.total = total
        self._spent = 0

    def spent(self) -> int:
        return self._spent

    def remaining(self) -> float:
        return math.inf if self.total is None else max(0, self.total - self._spent)

    def _sync(self, spent: int) -> None:
        self._spent = spent


class _RpcClient:
    def __init__(self, ch: Channel) -> None:
        self._ch = ch
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def reader_loop(self, on_done: asyncio.Future) -> None:
        while True:
            msg = await self._ch.recv()
            if msg is None:
                if not on_done.done():
                    on_done.set_exception(ConnectionError("host closed channel"))
                return
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                fut = self._pending.pop(mid)
                if not fut.done():
                    fut.set_result(msg)

    async def call(self, method: str, **params: Any) -> dict:
        self._next += 1
        mid = self._next
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ch.send({"t": "call", "id": mid, "method": method, **params})
        return await fut

    async def note(self, method: str, **params: Any) -> None:
        await self._ch.send({"t": "note", "method": method, **params})


async def _maybe_await(v: Any) -> Any:
    return await v if inspect.isawaitable(v) else v


def _build_namespace(rpc: _RpcClient, budget: _ChildBudget, args: Any, nesting: list[int]) -> dict:
    async def agent(prompt: str, opts: dict | None = None) -> Any:
        reply = await rpc.call("agent", prompt=prompt, opts=opts or {})
        if "spent" in reply:
            budget._sync(reply["spent"])
        if not reply.get("ok"):
            if reply.get("fatal"):
                raise _BudgetExhausted(reply.get("error", "budget exhausted"))
            raise RuntimeError(reply.get("error", "agent failed"))
        return reply.get("value")

    async def parallel(thunks: list[Callable[[], Awaitable[Any]]]) -> list[Any]:
        async def guard(thunk):
            try:
                return await _maybe_await(thunk())
            except _BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001 - failures -> None (barrier semantics)
                return None
        return await asyncio.gather(*(guard(t) for t in thunks))

    async def pipeline(items: list[Any], *stages: Callable[..., Any]) -> list[Any]:
        async def chain(item, index):
            prev = item
            for stage in stages:
                try:
                    prev = await _maybe_await(stage(prev, item, index))
                except _BudgetExhausted:
                    raise
                except Exception:  # noqa: BLE001
                    return None
            return prev
        return await asyncio.gather(*(chain(it, i) for i, it in enumerate(items)))

    def phase(title: str) -> None:
        asyncio.ensure_future(rpc.note("phase", title=str(title)))

    def log(message: str) -> None:
        asyncio.ensure_future(rpc.note("log", message=str(message)))

    async def workflow(name_or_ref: Any, sub_args: Any = None) -> Any:
        if nesting[0] >= 1:
            raise RuntimeError("workflow() cannot be nested more than one level deep")
        reply = await rpc.call("resolve", ref=name_or_ref)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "workflow resolve failed"))
        source = reply["source"]
        compiled = compile_script(source, filename=reply.get("file_path", "<child-workflow>"))
        nesting[0] += 1
        try:
            child_ns = _build_namespace(rpc, budget, sub_args, nesting)
            exec(compiled.code, child_ns)  # noqa: S102
            main = child_ns.get("main")
            if main is None or not inspect.iscoroutinefunction(main):
                raise RuntimeError("sub-workflow must define `async def main():`")
            return await main()
        finally:
            nesting[0] -= 1

    class _Console:
        def log(self, *a):
            log(" ".join(str(x) for x in a))
        error = warn = info = log

    return {
        "agent": agent,
        "parallel": parallel,
        "pipeline": pipeline,
        "phase": phase,
        "log": log,
        "workflow": workflow,
        "args": args,
        "budget": budget,
        "console": _Console(),
        "print": lambda *a, **k: log(" ".join(str(x) for x in a)),
    }


async def _run() -> int:
    ch = await Channel.from_fds(int(os.environ["OWF_IN"]), int(os.environ["OWF_OUT"]))
    init = await ch.recv()
    if not init or init.get("t") != "init":
        return 1
    budget = _ChildBudget(init.get("budget_total"))
    rpc = _RpcClient(ch)
    done: asyncio.Future = asyncio.get_event_loop().create_future()
    reader = asyncio.ensure_future(rpc.reader_loop(done))
    try:
        compiled = compile_script(init["script"], filename="<sandboxed-workflow>")
        ns = _build_namespace(rpc, budget, init.get("args"), [0])
        exec(compiled.code, ns)  # noqa: S102 - the whole point: run the script in the sandbox
        main = ns.get("main")
        if main is None or not inspect.iscoroutinefunction(main):
            raise RuntimeError("script must define `async def main():` as its entrypoint")
        result = await main()
        try:
            await ch.send({"t": "done", "value": result})
        except TypeError:
            await ch.send({"t": "done", "value": str(result)})
    except Exception as e:  # noqa: BLE001 - report back to host
        await ch.send({"t": "error", "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()})
    finally:
        reader.cancel()
        ch.close()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
