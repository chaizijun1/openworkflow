"""Tests for the MCP server bridge (Workflow tool) — exercised via the FastMCP-independent core."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MockBackend
from openworkflow.backends import AgentResult
from openworkflow.mcp_server import (
    SamplingBackend,
    SamplingToolBackend,
    execute_workflow,
    list_workflows,
    make_backend,
)
from openworkflow.tools import ToolBox


def run(**kw):
    return asyncio.run(execute_workflow(backend=MockBackend(), **kw))


# ----------------------------------------------------------------- Workflow tool core

def test_inline_script():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('hello ' + (args or ''))\n"
    )
    out = run(script=src, args="world")
    assert "hello world" in out
    assert "1 agent(s)" in out  # footer with accounting


def test_named_workflow():
    out = run(name="deep-research", args="x vs y")
    assert "agent(s)" in out
    assert "error" not in out.split("\n")[0].lower()


def test_unknown_name_reports_available():
    out = run(name="does-not-exist")
    assert out.startswith("error: no workflow named")
    assert "Available:" in out


def test_no_input_is_helpful_error():
    out = run()
    assert out.startswith("error: provide one of")
    assert "task" in out  # the natural-language fallback is advertised


# ----------------------------------------------------------------- task= auto-author (P3)

def test_task_param_designs_and_runs():
    """The root weak-model affordance: give natural language, server designs+validates+runs."""
    out = run(task="Research the tradeoffs of vector databases")
    assert "error" not in out.split("\n")[0].lower()
    assert "agent(s)" in out
    assert "auto-designed" in out  # footer notes it was authored, not hand-written


def test_task_param_pipeline_shape_for_per_item():
    out = run(task="Review each of these files for bugs", args=["a.py", "b.py"])
    assert "agent(s)" in out
    assert "error" not in out.split("\n")[0].lower()


def test_explicit_script_takes_precedence_over_task():
    src = "meta = {'name':'t'}\nasync def main():\n    return await agent('explicit')\n"
    out = run(script=src, task="this should be ignored")
    assert "explicit" in out
    assert "auto-designed" not in out  # the hand-written script ran, not a designed one


def test_task_nullish_is_ignored():
    # "null"/"" task must not trigger authoring; falls through to the helpful error
    out = run(task="null")
    assert out.startswith("error: provide one of")


def test_json_result_serialized():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return {'a': 1, 'b': [await agent('x')]}\n"
    )
    out = run(script=src)
    assert '"a": 1' in out


def test_list_workflows():
    txt = list_workflows()
    assert "deep-research" in txt and "bug-hunt" in txt


# ----------------------------------------------------------------- backend selection

def test_make_backend_env_override(monkeypatch):
    monkeypatch.setenv("OPENWORKFLOW_BACKEND", "mock")
    assert make_backend().name == "mock"


def test_make_backend_auto_with_key(monkeypatch):
    monkeypatch.setenv("OPENWORKFLOW_BACKEND", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert make_backend().name == "tool-agent"


def test_make_backend_auto_no_key_is_safe_mock(monkeypatch):
    # guardrail: auto must NOT silently use the client subscription via sampling
    monkeypatch.setenv("OPENWORKFLOW_BACKEND", "auto")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_backend(ctx=object()).name == "mock"


def test_sampling_is_opt_in_only(monkeypatch):
    monkeypatch.setenv("OPENWORKFLOW_BACKEND", "sampling-tools")
    assert make_backend(ctx=object()).name == "sampling-tools"
    monkeypatch.setenv("OPENWORKFLOW_BACKEND", "sampling")
    assert make_backend(ctx=object()).name == "sampling"


def test_sampling_guardrails_clamp():
    from openworkflow.mcp_server import (
        SAMPLING_DEFAULT_BUDGET,
        SamplingBackend,
        _sampling_guardrails,
    )
    # sampling backend -> concurrency forced to 1, default budget applied, warning returned
    budget, conc, warn = _sampling_guardrails(SamplingBackend(object()), None)
    assert conc == 1
    assert budget == SAMPLING_DEFAULT_BUDGET
    assert warn and "subscription" in warn.lower()
    # explicit budget respected, still clamped concurrency
    budget2, conc2, _ = _sampling_guardrails(SamplingBackend(object()), 12345)
    assert budget2 == 12345 and conc2 == 1
    # non-sampling backend -> untouched, no warning
    b3, c3, w3 = _sampling_guardrails(MockBackend(), None)
    assert b3 is None and w3 is None and c3 >= 1


# ----------------------------------------------------------------- sampling backend

def test_sampling_backend_uses_ctx_sample():
    class _FakeCtx:
        async def sample(self, prompt, max_tokens=4096):
            class R:
                text = f"sampled:{prompt}"
            return R()

    b = SamplingBackend(_FakeCtx())
    res = asyncio.run(b.run("do it", {}))
    assert res.value == "sampled:do it"
    assert res.output_tokens > 0


def test_sampling_full_workflow_via_client_model():
    """A whole workflow whose agents are served by the (faked) client model — like native."""
    class _FakeCtx:
        async def sample(self, prompt, max_tokens=4096):
            class R:
                text = "[client] " + prompt[:20]
            return R()

    out = asyncio.run(execute_workflow(
        script=("meta={'name':'t'}\n"
                "async def main():\n"
                "    return await parallel([lambda: agent('a'), lambda: agent('b')])\n"),
        backend=SamplingBackend(_FakeCtx()),
    ))
    assert "[client]" in out


def test_sampling_tools_executes_a_real_tool(tmp_path):
    """Method A with tools: client model (faked) drives a real Write via the ReAct loop."""
    target = tmp_path / "made_by_subagent.txt"

    class _ScriptedModelCtx:
        """Plays a model that issues a Write tool call, then finalizes — over MCP sampling."""
        def __init__(self):
            self._turn = 0

        async def sample(self, prompt, max_tokens=4096):
            self._turn += 1

            class R:
                pass
            r = R()
            if self._turn == 1:
                r.text = json.dumps({"tool": "Write",
                                     "input": {"file_path": str(target), "content": "hi"}})
            else:
                # the observation is now in the transcript; finish
                r.text = '{"final": "wrote the file"}'
            return r

    backend = SamplingToolBackend(_ScriptedModelCtx(), toolbox=ToolBox(cwd=str(tmp_path), confine=False))
    res = asyncio.run(backend.run("create the file", {}))
    assert target.exists() and target.read_text() == "hi"   # the tool really ran locally
    assert res.value == "wrote the file"                    # final answer from the client model
    assert res.output_tokens > 0


def test_sampling_tools_plain_text_fallback():
    """If the model just answers (no JSON), treat it as the final text."""
    class _Ctx:
        async def sample(self, prompt, max_tokens=4096):
            class R:
                text = "just a plain answer, no tool"
            return R()

    backend = SamplingToolBackend(_Ctx(), toolbox=ToolBox())
    res = asyncio.run(backend.run("hi", {}))
    assert "plain answer" in res.value


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
