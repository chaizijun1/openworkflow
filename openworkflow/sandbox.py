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
import json
import re
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

# Shown on format/validation failures so a weaker model can self-correct from the error alone:
# a concrete minimal script + the single most common mistake (wrapping the source).
MINIMAL_EXAMPLE = (
    'meta = {"name": "demo"}\n'
    "async def main():\n"
    '    return await parallel([lambda: agent("q1"), lambda: agent("q2")])'
)
SCRIPT_FORMAT_HINT = (
    "Pass raw source: the `script` arg must be raw Python — NOT wrapped in quotes, NOT JSON, "
    "NOT inside a ```python fence, and with no prose before it. The FIRST statement must be a "
    "`meta = {...}` dict literal. Minimal valid example:\n" + MINIMAL_EXAMPLE
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
        raise WorkflowScriptError("empty script.\n" + SCRIPT_FORMAT_HINT)
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
            "(name, description, phases).\n" + SCRIPT_FORMAT_HINT
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
        raise WorkflowScriptError(f"SyntaxError: {e}.\n" + SCRIPT_FORMAT_HINT) from e
    meta = _extract_meta(tree)
    _check_determinism(tree)
    code = compile(tree, filename=filename, mode="exec")
    return CompiledScript(source=source, code=code, meta=meta, filename=filename)


# --------------------------------------------------------------------------- #
# Tolerant ingestion (weak / local models).
#
# The contract is: ``script`` is RAW Python source whose first statement is ``meta = {...}``.
# Strong models honor that; local 27B-class models routinely mangle it — wrapping the source in
# markdown fences, JSON-encoding it, surrounding it with quotes (single/double/triple; real or
# escaped newlines), or prefixing it with prose. ``normalize_script`` peels those layers, in any
# combination, and returns the first variant that parses with ``meta = {...}`` first.
#
# Design rule (CLAUDE.md): tolerance only acts when the original is *unambiguously recoverable*.
# A clean script is returned unchanged, and if nothing qualifies the input is returned verbatim so
# the strict validator still emits its (example-bearing) error. The *decision* to invoke this is a
# caller policy (``OPENWORKFLOW_LENIENT``); the function itself is pure.
# --------------------------------------------------------------------------- #

# ``meta = {`` at the start of a line — the anchor for both prose-stripping and source-detection.
META_ASSIGN_RE = re.compile(r"(?m)^[ \t]*meta[ \t]*=[ \t]*\{")
# A markdown code fence with an optional language tag; captures the fenced body.
_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def _is_workflow_source(src: str) -> bool:
    """True iff ``src`` parses and its first statement is ``meta = {<dict literal>}``.

    The structural half of the script contract (``_extract_meta`` minus the literal-eval), used to
    decide whether an unwrapped candidate is "already a workflow".
    """
    try:
        tree = ast.parse(textwrap.dedent(src))
    except (SyntaxError, ValueError):
        return False
    if not tree.body:
        return False
    first = tree.body[0]
    return (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and isinstance(first.targets[0], ast.Name)
        and first.targets[0].id == "meta"
        and isinstance(first.value, ast.Dict)
    )


def _peel_fence(s: str) -> str | None:
    m = _FENCE_RE.search(s)
    return m.group(1) if m else None


def _peel_json(s: str) -> str | None:
    """A whole script that was JSON-encoded into a string (escaped ``\\n`` / ``\\"``)."""
    if not s or s[0] not in "\"'":
        return None
    try:
        v = json.loads(s)
    except Exception:  # noqa: BLE001 - any decode failure just means "not this layer"
        return None
    return v if isinstance(v, str) else None


def _peel_pyliteral(s: str) -> str | None:
    """A script wrapped in a Python string literal (single/triple quotes, escaped newlines)."""
    if not s or s[0] not in "\"'":
        return None
    try:
        v = ast.literal_eval(s)
    except Exception:  # noqa: BLE001
        return None
    return v if isinstance(v, str) else None


def _peel_quotes(s: str) -> str | None:
    """Literally strip a matched pair of surrounding quotes (handles real newlines inside)."""
    for q in ('"""', "'''"):
        if len(s) >= 2 * len(q) and s.startswith(q) and s.endswith(q):
            return s[len(q):-len(q)]
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    return None


def _peel_to_meta(s: str) -> str | None:
    """Drop leading prose/imports: slice from the first ``meta = {`` at a line start."""
    m = META_ASSIGN_RE.search(s)
    if m and m.start() > 0:
        return s[m.start():]
    return None


_PEELS = (_peel_fence, _peel_json, _peel_pyliteral, _peel_quotes, _peel_to_meta)


def normalize_script(raw: str, *, max_candidates: int = 64) -> str:
    """Best-effort recovery of raw workflow source from weak-model manglings.

    Breadth-first peeling: each layer (fence / JSON / Python-literal / quotes / prose) is tried in
    every order, and the first candidate whose first statement is ``meta = {...}`` and that parses
    wins (fewest peels first, so the least-transformed valid source is chosen). A clean script is
    returned unchanged; if nothing qualifies, ``raw`` is returned so the strict validator runs.
    """
    if not isinstance(raw, str):
        return raw
    seen: set[str] = set()
    queue: list[str] = [raw.strip()]
    examined = 0
    while queue and examined < max_candidates:
        s = queue.pop(0)
        if s in seen:
            continue
        seen.add(s)
        examined += 1
        if _is_workflow_source(s):
            return textwrap.dedent(s).strip() + "\n"
        for peel in _PEELS:
            try:
                nxt = peel(s)
            except Exception:  # noqa: BLE001 - a misbehaving peel never aborts normalization
                nxt = None
            if nxt is not None:
                nxt = nxt.strip()
                if nxt and nxt not in seen:
                    queue.append(nxt)
    return raw
