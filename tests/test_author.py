"""Tests for the autonomous author layer (design + design→run), using MockBackend."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass
from typing import Any

import pytest

from openworkflow import MockBackend, design_workflow, do_task
from openworkflow.backends import AgentBackend, AgentResult
from openworkflow.sandbox import WorkflowScriptError, compile_script


def test_scaffold_designs_valid_fanout():
    d = asyncio.run(design_workflow("Research the tradeoffs of vector databases", backend=MockBackend()))
    assert d.authored_by == "scaffold"
    assert d.compiled.meta["name"] == "auto-fanout"
    compile_script(d.script)  # must be valid


def test_scaffold_picks_pipeline_for_per_item_tasks():
    d = asyncio.run(design_workflow("Review each of these files for bugs", backend=MockBackend()))
    assert d.compiled.meta["name"] == "auto-pipeline"


def test_do_task_designs_then_runs():
    result, design = asyncio.run(
        do_task("Summarize the pros and cons of microservices", backend=MockBackend(), quiet=True)
    )
    assert design.authored_by == "scaffold"
    assert result.agent_count >= 1
    assert result.result  # produced something


# ---- simulate a real authoring model with a fake backend that returns code ----

@dataclass
class _CodeBackend(AgentBackend):
    """Pretends to be an LLM that authors a workflow script."""

    name: str = "fake-llm"
    fail_first: bool = False
    _calls: int = 0

    async def run(self, prompt: str, opts: dict[str, Any]) -> AgentResult:
        self._calls += 1
        if self.fail_first and self._calls == 1:
            bad = "x = 1\nmeta = {'name': 'bad'}\nasync def main():\n    return 1"  # meta not first
            return AgentResult(value=f"```python\n{bad}\n```", output_tokens=10)
        good = (
            "meta = {'name': 'authored', 'description': 'd', 'phases': ['go']}\n"
            "async def main():\n"
            "    phase('go')\n"
            "    return await agent('do the task: ' + (args or ''))\n"
        )
        return AgentResult(value=f"```python\n{good}\n```", output_tokens=10)


def test_llm_authoring_path():
    d = asyncio.run(design_workflow("anything", backend=_CodeBackend()))
    assert d.authored_by == "llm"
    assert d.attempts == 0
    assert d.compiled.meta["name"] == "authored"


def test_llm_authoring_repairs_on_validation_error():
    d = asyncio.run(design_workflow("anything", backend=_CodeBackend(fail_first=True)))
    assert d.authored_by == "llm"
    assert d.attempts == 1  # one repair round


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
