"""Weak-model tolerance: the bad ``script`` args local 27B-class models actually emit.

These are the failure modes from ``LOCAL_LLM_HARDENING.md`` §3/§P1. The contract is that
``script`` is *raw Python source* whose first statement is ``meta = {...}``. Weak models mangle
that into quote-wrapped / JSON-encoded / markdown-fenced / prose-prefixed blobs. The fixtures in
``tests/fixtures/weak_inputs/`` are byte-exact captures of each mangle; ``normalize_script`` must
recover compilable source from every one — while leaving a clean script (the strong-model path)
untouched, and restoring strict behavior when ``OPENWORKFLOW_LENIENT=0``.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MockBackend
from openworkflow.mcp_server import _nullish, _sanitize_args, execute_workflow
from openworkflow.sandbox import (
    _is_workflow_source,
    WorkflowScriptError,
    compile_script,
    normalize_script,
)

FIXDIR = Path(__file__).parent / "fixtures" / "weak_inputs"
FIXTURES = sorted(FIXDIR.glob("*.txt"))

# expected meta["name"] recovered from each fixture (pins the rescue, not just "it compiled")
EXPECTED = {
    "json_encoded.txt": "json-encoded",
    "double_quote_wrapped.txt": "dq-wrapped",
    "single_quote_wrapped.txt": "sq-wrapped",
    "triple_quote_wrapped.txt": "triple-wrapped",
    "single_quote_escaped.txt": "sq-escaped",
    "md_fence_python.txt": "md-python",
    "md_fence_bare.txt": "md-bare",
    "prose_before_meta.txt": "prose-before",
    "prose_and_fence.txt": "prose-fence",
    "fence_inside_quotes.txt": "fence-in-quotes",
    "comment_blanklines_before_meta.txt": "comment-before",
}

CLEAN = (
    "meta = {'name': 'clean', 'description': 'd'}\n"
    "async def main():\n"
    "    return await agent('do the thing: ' + (args or ''))\n"
)


def test_fixtures_present():
    # guard against an empty/missing fixtures dir silently passing the parametrized test
    assert len(FIXTURES) >= 8, f"expected >=8 weak-input fixtures, found {len(FIXTURES)}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_normalize_rescues_every_fixture(fixture):
    raw = fixture.read_text(encoding="utf-8")
    src = normalize_script(raw)
    compiled = compile_script(src)  # must not raise
    assert isinstance(compiled.meta.get("name"), str) and compiled.meta["name"]
    if fixture.name in EXPECTED:
        assert compiled.meta["name"] == EXPECTED[fixture.name]


def test_clean_script_untouched():
    """Strong-model path: a valid script must survive normalize_script semantically intact."""
    src = normalize_script(CLEAN)
    a = compile_script(src).meta
    b = compile_script(CLEAN).meta
    assert a == b == {"name": "clean", "description": "d"}
    assert "async def main" in src and "agent(" in src


def test_normalize_passthrough_non_string():
    # robustness: a non-string must not blow up (returned as-is)
    assert normalize_script(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("prefix", [
    "import os\n",
    "from __future__ import annotations\n",
    "X = 1\n",
    "helper = lambda x: x\n",
])
def test_leading_valid_statement_not_silently_dropped(prefix):
    """Tolerance must not DROP real code. Slicing off leading *prose* is unambiguous; slicing off a
    leading *valid statement* (e.g. `import os`) silently changes the program, which violates the
    'only recover when unambiguous' rule. Such input should be left for the strict validator, not
    rewritten into a different (compiling) script."""
    src = prefix + "meta = {'name': 'x'}\nasync def main():\n    return 1\n"
    norm = normalize_script(src)
    # if normalize claims it's a workflow, it must NOT have dropped the author's leading code
    if _is_workflow_source(norm):
        assert prefix.strip() in norm, f"normalize dropped leading code: {norm!r}"


def test_leading_prose_still_sliced():
    """Conversely, leading PROSE (not valid Python) is still unambiguously strippable."""
    src = "Here is the workflow you asked for:\n\nmeta = {'name': 'p'}\nasync def main():\n    return 1\n"
    assert compile_script(normalize_script(src)).meta["name"] == "p"


def test_multiple_fences_picks_the_workflow_one():
    """A decoy fence (prose / bash) before the real ```python fence must not defeat rescue."""
    raw = ('Here is the plan:\n```\nstep 1\nstep 2\n```\n'
           'And the code:\n```python\nmeta = {"name": "twofence"}\n'
           'async def main():\n    return await agent("hi")\n```\n')
    assert compile_script(normalize_script(raw)).meta["name"] == "twofence"

    raw2 = ('```bash\npip install openworkflow\n```\n'
            '```python\nmeta = {"name": "twofence2"}\n'
            'async def main():\n    return await agent("hi")\n```')
    assert compile_script(normalize_script(raw2)).meta["name"] == "twofence2"


def test_leading_banned_import_not_rescued_into_nondeterministic_pass():
    """Determinism guarantee: slicing `import random` away would let _check_determinism pass a
    nondeterministic script the strict validator rejects. Tolerance must NOT do that."""
    raw = ("import random\nmeta = {'name': 'x'}\n"
           "async def main():\n    return random.random()\n")
    with pytest.raises(WorkflowScriptError):
        compile_script(normalize_script(raw))


def test_leading_bom_is_stripped():
    """A UTF-8 BOM is unambiguous encoding noise (str.strip() doesn't remove it) — recover it."""
    src = "﻿meta = {'name': 'bom'}\nasync def main():\n    return 1\n"
    assert compile_script(normalize_script(src)).meta["name"] == "bom"
    # combined with a fence too (BOM outside a markdown wrap)
    src2 = "﻿```python\nmeta = {'name': 'bom2'}\nasync def main():\n    return 1\n```\n"
    assert compile_script(normalize_script(src2)).meta["name"] == "bom2"


# ----------------------------------------------------------------- _sanitize_args wiring

def test_nullish_maps_string_null_to_none():
    for s in ("null", "NULL", "none", "None", "undefined", "", "  ", "nil"):
        # "nil" is NOT nullish — only the documented tokens map to None
        expected = None if s.strip().lower() in ("null", "none", "undefined", "") else s
        assert _nullish(s) == expected


def test_sanitize_string_null_defaults():
    # weak models pass omitted optional args as the literal string "null"
    assert _sanitize_args("null", None, "null") == (None, None, None)
    assert _sanitize_args("none", "undefined", "") == (None, None, None)


def test_sanitize_unwraps_wrapped_script():
    raw = (FIXDIR / "double_quote_wrapped.txt").read_text(encoding="utf-8")
    script, name, path = _sanitize_args(raw, None, None)
    assert name is None and path is None
    compile_script(script)  # the unwrapped script compiles


def test_sanitize_reroutes_source_in_name():
    """A weak model sometimes dumps the whole source into `name` (or scriptPath)."""
    raw = (FIXDIR / "md_fence_python.txt").read_text(encoding="utf-8")
    script, name, path = _sanitize_args(None, raw, None)
    assert script is not None and name is None
    assert compile_script(script).meta["name"] == "md-python"

    script2, name2, path2 = _sanitize_args(None, None, raw)
    assert script2 is not None and path2 is None
    compile_script(script2)


def test_sanitize_keeps_real_name():
    # a plain saved-workflow name must NOT be rerouted to script
    assert _sanitize_args(None, "deep-research", None) == (None, "deep-research", None)


# ----------------------------------------------------------------- lenient switch (P5 seam)

def test_lenient_off_restores_strict(monkeypatch):
    monkeypatch.setenv("OPENWORKFLOW_LENIENT", "0")
    raw = (FIXDIR / "double_quote_wrapped.txt").read_text(encoding="utf-8")
    # strict mode: no unwrapping, no nullish coercion -> args pass through verbatim
    assert _sanitize_args(raw, None, "null") == (raw, None, "null")


def test_lenient_on_by_default(monkeypatch):
    monkeypatch.delenv("OPENWORKFLOW_LENIENT", raising=False)
    assert _sanitize_args("null", None, None) == (None, None, None)


@pytest.mark.parametrize("val,expected", [
    (None, True),       # unset -> lenient (friendly by default)
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), ("", True),
    ("0", False), ("false", False), ("False", False), ("no", False), ("off", False), (" off ", False),
])
def test_lenient_env_spellings(monkeypatch, val, expected):
    from openworkflow.mcp_server import _lenient
    if val is None:
        monkeypatch.delenv("OPENWORKFLOW_LENIENT", raising=False)
    else:
        monkeypatch.setenv("OPENWORKFLOW_LENIENT", val)
    assert _lenient() is expected


def test_lenient_default_rescues_end_to_end(monkeypatch):
    monkeypatch.delenv("OPENWORKFLOW_LENIENT", raising=False)
    raw = (FIXDIR / "json_encoded.txt").read_text(encoding="utf-8")
    out = asyncio.run(execute_workflow(script=raw, backend=MockBackend()))
    assert "error" not in out.split("\n")[0].lower() and "agent(s)" in out


def test_lenient_off_fails_end_to_end(monkeypatch):
    monkeypatch.setenv("OPENWORKFLOW_LENIENT", "0")
    raw = (FIXDIR / "json_encoded.txt").read_text(encoding="utf-8")
    out = asyncio.run(execute_workflow(script=raw, backend=MockBackend()))
    assert out.startswith("error:")  # strict contract: mangled input is rejected


# ----------------------------------------------------------------- P2: errors that teach

def _err(src):
    with pytest.raises(WorkflowScriptError) as ei:
        compile_script(src)
    return str(ei.value)


def test_meta_error_carries_example_and_raw_hint():
    msg = _err("x = 1\nasync def main():\n    return 1\n")  # meta not first
    assert "meta = {" in msg          # a concrete correct example
    assert "raw source" in msg.lower()  # tells the model not to wrap it


def test_syntaxerror_carries_example():
    msg = _err("this is not python ??")
    assert "meta = {" in msg and "raw source" in msg.lower()


def test_empty_script_carries_example():
    msg = _err("   \n  \n")
    assert "meta = {" in msg


def test_end_to_end_unrecoverable_error_teaches():
    """The tool's returned error string (what the model actually sees) teaches the fix."""
    out = asyncio.run(execute_workflow(script="x = 1\nasync def main():\n    return 1\n",
                                       backend=MockBackend()))
    assert out.startswith("error:")
    assert "meta = {" in out and "raw source" in out.lower()


# ----------------------------------------------------------------- end-to-end through the tool

@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_every_fixture_runs_end_to_end(fixture):
    """Regression: every mangled input, through the real tool entry, reaches the runtime and runs.

    Stronger than the unit normalize_script check — it guards the whole wiring
    (_sanitize_args -> normalize_script -> run_workflow) against future refactors.
    """
    raw = fixture.read_text(encoding="utf-8")
    out = asyncio.run(execute_workflow(script=raw, backend=MockBackend()))
    assert "error" not in out.split("\n")[0].lower(), out
    assert "agent(s)" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
