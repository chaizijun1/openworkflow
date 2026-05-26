"""Script loading, validation and the restricted namespace.

Mirrors the original's script contract:

  * The FIRST statement must be a ``meta = {...}`` literal (pure literal, no computed values).
    In the original it is ``export const meta = { name, description, phases }``.
  * Clocks/RNG are unavailable, because "Date.now() / new Date() are unavailable in workflow
    scripts (breaks resume)". We reject obvious nondeterministic calls at validation time.
  * The script defines ``async def main():`` as the entrypoint (the orchestration body). The
    six primitives plus ``args``/``budget``/``console`` are injected as module globals, so the
    body uses them directly — matching the JS where they are in scope.

This is NOT a security sandbox (Python ``exec`` cannot be one without a subprocess/seccomp);
it is a *contract + determinism* sandbox, which is what the resume/replay model needs.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from typing import Any

# Attribute calls that introduce nondeterminism and would break journal replay.
BANNED_ATTRS = {
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
}
# Module names whose use we flag wholesale (RNG / wall clock).
BANNED_MODULES = {"random", "secrets", "uuid"}

DETERMINISM_HINT = (
    "Date/time/RNG are unavailable in workflow scripts (breaks resume). "
    "Stamp results after the workflow returns, or pass values in via `args`."
)


class WorkflowScriptError(Exception):
    pass


@dataclass
class CompiledScript:
    source: str
    code: Any  # code object
    meta: dict[str, Any]
    filename: str


def _extract_meta(tree: ast.Module) -> dict[str, Any]:
    if not tree.body:
        raise WorkflowScriptError("empty script")
    first = tree.body[0]
    ok = (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and isinstance(first.targets[0], ast.Name)
        and first.targets[0].id == "meta"
        and isinstance(first.value, ast.Dict)
    )
    if not ok:
        raise WorkflowScriptError(
            "the FIRST statement must be a `meta = {...}` dict literal "
            "(name, description, phases)"
        )
    try:
        meta = ast.literal_eval(first.value)
    except ValueError as e:
        raise WorkflowScriptError(f"meta must be a pure literal (no computed values): {e}") from e
    if not isinstance(meta, dict) or "name" not in meta:
        raise WorkflowScriptError("meta must be a dict with at least a `name`")
    return meta


def _check_determinism(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    raise WorkflowScriptError(f"`import {alias.name}` is banned — {DETERMINISM_HINT}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                raise WorkflowScriptError(f"`from {node.module} import ...` is banned — {DETERMINISM_HINT}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if isinstance(node.func.value, ast.Name):
                if (node.func.value.id, attr) in BANNED_ATTRS:
                    raise WorkflowScriptError(
                        f"`{node.func.value.id}.{attr}()` is banned — {DETERMINISM_HINT}"
                    )


def compile_script(source: str, filename: str = "workflow.py") -> CompiledScript:
    source = textwrap.dedent(source)
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        raise WorkflowScriptError(f"SyntaxError: {e}") from e
    meta = _extract_meta(tree)
    _check_determinism(tree)
    code = compile(tree, filename=filename, mode="exec")
    return CompiledScript(source=source, code=code, meta=meta, filename=filename)
