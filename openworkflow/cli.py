"""Command-line entrypoint.

    openworkflow list                       list saved workflows (user/project/built-in)
    openworkflow run <script.py> [opts]     run a script file
    openworkflow run-name <name> [opts]     run a saved workflow by name

Options:
    --args JSON        value passed to the script's `args` global (JSON-decoded)
    --budget N         hard output-token ceiling for the run
    --concurrency N    max concurrent agent() calls (default 8)
    --backend NAME     mock | anthropic   (default: anthropic if ANTHROPIC_API_KEY else mock)
    --journal PATH     journal file for resume
    --resume           replay completed agent() results from the journal
    --quiet            suppress the progress narrator
    --json             print the final result as JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .backends import AnthropicBackend, MockBackend, ToolAgentBackend, default_backend
from .registry import WorkflowRegistry
from .runtime import run_workflow
from .sandbox import WorkflowScriptError


def _pick_backend(name: str | None):
    if name == "mock":
        return MockBackend()
    if name == "anthropic":
        return AnthropicBackend()
    if name == "tool":
        return ToolAgentBackend()
    return default_backend()


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--args", default=None, help="JSON value for the script's `args` global")
    p.add_argument("--budget", type=int, default=None, help="hard output-token ceiling")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--backend", choices=["mock", "anthropic", "tool"], default=None)
    p.add_argument("--journal", default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--secure", action="store_true",
                   help="run the script in an OS sandbox (sandbox-exec/bwrap); no net/fs egress")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openworkflow", description="Run Claude-style workflows.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list saved workflows")

    p_run = sub.add_parser("run", help="run a script file")
    p_run.add_argument("script")
    _add_run_opts(p_run)

    p_name = sub.add_parser("run-name", help="run a saved workflow by name")
    p_name.add_argument("name")
    _add_run_opts(p_name)

    p_design = sub.add_parser("design", help="autonomously design a workflow for a task (print it)")
    p_design.add_argument("task")
    p_design.add_argument("--backend", choices=["mock", "anthropic", "tool"], default=None)

    p_do = sub.add_parser("do", help="autonomously design a workflow for a task, then run it")
    p_do.add_argument("task")
    _add_run_opts(p_do)

    ns = parser.parse_args(argv)

    if ns.cmd == "list":
        reg = WorkflowRegistry()
        defs = reg.all()
        if not defs:
            print("(no workflows found)")
            return 0
        width = max(len(n) for n in defs)
        for name in sorted(defs):
            d = defs[name]
            print(f"{name.ljust(width)}  [{d.source}]  {d.description}")
        return 0

    if ns.cmd == "design":
        from .author import design_workflow
        from .registry import WorkflowRegistry as _Reg
        backend = _pick_backend(ns.backend)
        try:
            design = asyncio.run(
                design_workflow(ns.task, backend=backend, registry=_Reg())
            )
        except WorkflowScriptError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(
            f"# designed by {design.authored_by}, {design.attempts} repair attempt(s)\n",
            file=sys.stderr,
        )
        print(design.script)
        return 0

    if ns.cmd == "do":
        from .runtime import do_task
        backend = _pick_backend(ns.backend)
        args_val = json.loads(ns.args) if ns.args else None
        try:
            result, design = asyncio.run(
                do_task(
                    ns.task,
                    args=args_val,
                    backend=backend,
                    budget_total=ns.budget,
                    concurrency=ns.concurrency,
                    quiet=ns.quiet,
                    secure=ns.secure,
                )
            )
        except (WorkflowScriptError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(
            f"\n— done: designed '{design.compiled.meta['name']}' by {design.authored_by}, "
            f"{result.agent_count} agent(s), {result.spent_tokens} output tokens",
            file=sys.stderr,
        )
        if ns.json:
            print(json.dumps(result.result, ensure_ascii=False, indent=2, default=str))
        else:
            print(result.result)
        return 0

    args_val = json.loads(ns.args) if ns.args else None
    backend = _pick_backend(ns.backend)

    try:
        if ns.cmd == "run":
            with open(ns.script, encoding="utf-8") as f:
                source = f.read()
            source_or_def = source
        else:  # run-name
            reg = WorkflowRegistry()
            wdef = reg.get(ns.name)
            if wdef is None:
                avail = ", ".join(sorted(reg.all())) or "(none)"
                print(f"error: no workflow named '{ns.name}'. Available: {avail}", file=sys.stderr)
                return 2
            source_or_def = wdef

        result = asyncio.run(
            run_workflow(
                source_or_def,
                args=args_val,
                backend=backend,
                budget_total=ns.budget,
                concurrency=ns.concurrency,
                journal_path=ns.journal,
                resume=ns.resume,
                quiet=ns.quiet,
                secure=ns.secure,
            )
        )
    except (WorkflowScriptError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # final report to stderr, result to stdout
    print(
        f"\n— done: {result.agent_count} agent(s), {result.spent_tokens} output tokens"
        f"{', ' + str(len(result.failures)) + ' failure(s)' if result.failures else ''}"
        f"{', ' + str(len(result.drift)) + ' drift' if result.drift else ''}",
        file=sys.stderr,
    )
    for d in result.drift:
        print(f"  drift: {d}", file=sys.stderr)

    if ns.json:
        print(json.dumps(result.result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result.result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
