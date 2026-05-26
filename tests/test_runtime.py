"""Tests for the orchestration runtime, using the zero-cost MockBackend."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MockBackend
from openworkflow.budget import Budget
from openworkflow.runtime import BudgetExhausted, run_workflow
from openworkflow.sandbox import WorkflowScriptError, compile_script


def run(src, **kw):
    return asyncio.run(run_workflow(src, backend=MockBackend(), quiet=True, **kw))


# ---------------------------------------------------------------- script contract

def test_meta_must_be_first_statement():
    with pytest.raises(WorkflowScriptError):
        compile_script("x = 1\nmeta = {'name': 'bad'}\nasync def main(): return 1")


def test_meta_must_be_literal():
    with pytest.raises(WorkflowScriptError):
        compile_script("meta = {'name': 'x', 'n': 1 + 1}\nasync def main(): return 1")


def test_determinism_ban():
    src = "meta = {'name':'x'}\nimport time\nasync def main(): return time.time()"
    with pytest.raises(WorkflowScriptError):
        compile_script(src)


def test_requires_async_main():
    src = "meta = {'name':'x'}\ndef main(): return 1"
    with pytest.raises(WorkflowScriptError):
        run(src)


# ---------------------------------------------------------------- primitives

def test_agent_returns_value_and_counts():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('hello world', {'label': 'greet'})\n"
    )
    res = run(src)
    assert res.agent_count == 1
    assert "greet" in res.result


def test_agent_schema_returns_dict():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('x', {'schema': {'type':'object','properties':{'a':{'type':'integer'}}}})\n"
    )
    res = run(src)
    assert isinstance(res.result, dict) and "a" in res.result


def test_parallel_is_barrier_and_failures_become_none():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    async def boom():\n"
        "        raise ValueError('x')\n"
        "    return await parallel([lambda: agent('a'), boom, lambda: agent('b')])\n"
    )
    res = run(src)
    assert len(res.result) == 3
    assert res.result[1] is None
    assert res.result[0] and res.result[2]
    assert res.failures  # one failure recorded


def test_pipeline_threads_stages():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    async def s1(prev, item, i): return await agent(f'find {item} {i}')\n"
        "    async def s2(prev, item, i): return await agent(f'verify {item}: {prev}')\n"
        "    return await pipeline(['x','y'], s1, s2)\n"
    )
    res = run(src)
    assert len(res.result) == 2
    assert res.agent_count == 4  # 2 items x 2 stages


# ---------------------------------------------------------------- budget

def test_budget_hard_ceiling_raises():
    # MockBackend spends ~len(text)//4 tokens; a tiny budget should trip on the 2nd call.
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    out = []\n"
        "    for i in range(50):\n"
        "        out.append(await agent('some prompt number ' + str(i)))\n"
        "    return out\n"
    )
    with pytest.raises(BudgetExhausted):
        run(src, budget_total=5)


def test_budget_remaining_infinite_without_total():
    b = Budget()
    assert b.remaining() == float("inf")
    assert not b.exhausted()


# ---------------------------------------------------------------- journal / resume

def test_journal_replay_skips_recompute(tmp_path):
    jpath = str(tmp_path / "j.jsonl")
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('stable prompt', {'label':'L'})\n"
    )
    first = run(src, journal_path=jpath)
    # second run resumes; the agent call is a journal hit (no new spend)
    second = run(src, journal_path=jpath, resume=True)
    assert second.result == first.result
    assert second.spent_tokens == 0  # replayed, nothing spent


def test_drift_detected_when_branch_changes(tmp_path):
    jpath = str(tmp_path / "j.jsonl")
    src_a = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('prompt A', {'label':'L'})\n"
    )
    src_b = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('prompt B', {'label':'L'})\n"  # different prompt -> different key
    )
    run(src_a, journal_path=jpath)
    res = run(src_b, journal_path=jpath, resume=True)
    assert res.drift  # the cached 'prompt A' result was never reached


# ---------------------------------------------------------------- nested workflow

def test_workflow_nesting_one_level(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "meta = {'name':'child'}\nasync def main():\n    return await agent('child task')\n"
    )
    src = (
        "meta = {'name':'parent'}\n"
        "async def main():\n"
        "    return await workflow({'scriptPath': %r})\n" % str(child)
    )
    res = run(src)
    assert "child task" in res.result
    assert res.agent_count == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
