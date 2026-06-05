"""Built-in workflow templates (P4): the reliable, name-callable workflows weak models prefer.

Calling a pre-written workflow by ``name`` is the most robust path for a weak model — no script
authoring at all. These tests assert the shipped templates are registered, compile against the
contract, and run under MockBackend across the arg shapes a model is likely to send.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MockBackend
from openworkflow.mcp_server import execute_workflow, list_workflows
from openworkflow.registry import WorkflowRegistry
from openworkflow.sandbox import compile_script

WORKFLOWS_DIR = Path(__file__).parent.parent / "workflows"


def test_builtin_templates_registered():
    names = set(WorkflowRegistry().all())
    assert {"deep-research", "bug-hunt", "code-review", "vote"} <= names


def test_all_builtins_compile():
    for p in sorted(WORKFLOWS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        compile_script(p.read_text(encoding="utf-8"), filename=str(p))  # must satisfy the contract


def test_list_workflows_nudges_name_usage():
    txt = list_workflows()
    assert "code-review" in txt and "vote" in txt and "deep-research" in txt
    # the listing should steer the model toward name= invocation
    assert "name=" in txt or "Workflow(name" in txt


@pytest.mark.parametrize(
    "name,args",
    [
        ("code-review", ["a.py", "b.py"]),
        ("code-review", {"files": ["x.py"], "focus": "security"}),
        ("code-review", "single_file.py"),
        ("code-review", None),
        ("vote", "Is 113 prime?"),
        ("vote", {"question": "pick one", "n": 4}),
        ("vote", None),
    ],
)
def test_template_runs_across_arg_shapes(name, args):
    out = asyncio.run(execute_workflow(name=name, args=args, backend=MockBackend()))
    assert "agent(s)" in out
    assert "error" not in out.split("\n")[0].lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
